# 提案：图片审核决定的「同库权威保存」（方案 A）

> **状态：提案 —— 未实现、未写迁移、未部署、未动生产。**
> 基线：`c42157a`（您第十二批复验后的最终 HEAD）。用途：请您判断这条路线是否可行、边界是否够。
> 本文件**不是修复补丁**；确认后我才实现，并按您要求另行提交迁移/兼容/备份/回滚材料。

---

## 0. 一页摘要

| 项 | 内容 |
| --- | --- |
| 问题 | 决定状态被拆在**两个存储**（`image_allowlist` 行 + `moderation.db.rejections.json`），两者没有共同提交边界。三轮反例（R1 归属、R2 穿透、R2-2 再读）都源于此；您已明确"不能靠第三次、第四次读取关闭" |
| 方案 | **决定 / 来源 / 操作版本 / 生效位在同一个 SQLite 事务内权威保存**；JSON 降级为**可重试的派生导出** |
| 关键效果 | 补偿机制**整体删除**（不再有"撤销已生效决定"这条路），C03 这一整类并发问题消失；导出失败变成"决定已生效、导出待重试"的**显式报告**，而不是回滚 |
| 代价 | 一个**additive 迁移**（新增列）+ 一个**幂等回填脚本** + 若干工具读写路径调整 + **您三个原件里编码"补偿恢复"的用例需要重新定义验收语义**（清单见 §8） |
| 运行时影响 | **零**：判定仍读 `image_allowlist.enabled=1`（`app/moderation/image_hash.py:167-169`），判定逻辑/阈值/enforce 均不动 |
| 需要您拍板 | §11 的 6 个问题（表形态、rejected 是否必须落行、导出失败的语义变更确认、回填方式、迁移与部署是否同窗口、回滚口径） |

---

## 1. 为什么建议停止"补偿"路线

| 轮次 | 您的反例 | 我们的补丁 | 结果 |
| --- | --- | --- | --- |
| 11 | 提交后 `after` 吸收外部行 | 写事务内产出**归属凭据** | 关闭 |
| 11 | 补偿覆盖后来成功决定 | 补偿提交前**再读一次**快照 | 缩小窗口，未关闭 |
| 12 | 第二次读与 commit 之间被穿过 | —— | 您判定：**读—判定—提交**与"别处的 JSON 发布"没有共同边界，再读无解 |

结论：问题不在读得够不够多，而在**两个存储之间没有共同的提交边界**。方案 A 直接把权威收到一个事务里。

---

## 2. 目标架构

1. **唯一权威** = 一次 SQLite 事务内的 `(决定状态, 来源, 操作版本, 生效位)`；
2. **JSON 快照 = 派生导出**：由权威状态生成，可随时重放；**不再是判断依据**；
3. **失败语义**：
   - 事务提交成功 → 决定**已生效**（无论导出是否成功）；
   - 导出失败 → 打印 `DECISIONS_COMMITTED_EXPORT_PENDING`、**非零退出**、不撤销任何决定；
   - 提供 `--export-only` 重放导出、`--verify-authority` 对账（DB ↔ JSON 一致性）；
4. **运行时判定路径完全不变**（仍读 `enabled=1`）。

---

## 3. 数据模型（推荐 **A2：扩展 `image_allowlist`**，备选 A1 新表）

现有 `image_allowlist`（`app/models.py:201-219`）：`id, phash, note, source, hit_count, enabled, created_at, created_by`，`phash` 唯一。

**A2（推荐）新增列（SQLite `ALTER TABLE ADD COLUMN`，常数时间、不重写表）：**

```sql
ALTER TABLE image_allowlist ADD COLUMN decision_state    VARCHAR(16) NOT NULL DEFAULT '';   -- 'allowed' | 'rejected' | ''
ALTER TABLE image_allowlist ADD COLUMN decision_source   VARCHAR(96) NOT NULL DEFAULT '';   -- 例 review:batch-20260102T000000Z / exclude / seed
ALTER TABLE image_allowlist ADD COLUMN decision_operator VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE image_allowlist ADD COLUMN decision_version  INTEGER    NOT NULL DEFAULT 0;      -- 单调；每次人工决定 +1
ALTER TABLE image_allowlist ADD COLUMN decided_at        DATETIME;
CREATE INDEX IF NOT EXISTS ix_image_allowlist_decision_state ON image_allowlist(decision_state);
```

**语义收敛（关键变化）**：**每个有决定的哈希都有一行**——
放行 → `enabled=1, decision_state='allowed'`；撤回 → `enabled=0, decision_state='rejected'`。
（现在"只被撤回、从未进过白名单"的哈希只在 JSON 里；A2 让它们在库里有行。）

**并发/版本**：每次决定 = 一个事务，先读该行 `decision_version` 做 **CAS**，成功才写入 `version+1`；
更晚的决定必然 bump 版本，**旧操作永远无法覆盖新决定**（不需要补偿、也不需要"再读一次"）。

**A1（备选）**：新建 `image_review_decisions` 表（同样的 5 列 + `phash` 主键），
`image_allowlist` 仅作运行时的"生效名单"。优点：职责更清；缺点：出现"第二张真相表"，
两者仍需同事务双写。我们倾向 A2（少一处不变量），请您裁定。

---

## 4. 写入路径（事务边界）

```
apply_review_decisions（单事务）：
  BEGIN IMMEDIATE
    for 每个决定: 读行 → 校验 decision_version（CAS）→ 写 state/source/operator/version/decided_at/enabled
    （同一事务内完成，无跨存储步骤）
  COMMIT                     ← 此刻"决定已生效"
  ↓（事务之外，可失败、可重试）
  export_snapshot()          ← 由库生成 JSON；失败 → DECISIONS_COMMITTED_EXPORT_PENDING（非零）
```

- **删除** `_compensate` / 归属凭据 / 提交后再读 的全部机制；
- `decision_lock`（跨进程锁）**保留**，仅用于让导出顺序确定、避免多个进程同时重写同一 JSON；
  即使没有它也不会产生分歧（库是唯一权威）；
- 现有 `record_rejection` / `record_approval` 改为**仅导出**语义（不再承载权威）。

---

## 5. 迁移与回填

1. **迁移（纯 DDL）**：`alembic/versions/e1c7d4b8a902_add_decision_authority.py`
   - `upgrade`：§3 的 5 列 + 1 索引；**不动数据**；
   - `downgrade`：`batch_alter_table` 去列（可选执行，见 §7）。
2. **回填（独立脚本，幂等）**：`scripts/backfill_image_decisions.py`
   - `--dry-run`（默认）打印将写入的行与冲突；`--apply` 才写；
   - 来源：现有 `moderation.db.rejections.json`（69 条）+ `image_allowlist` 现有行；
   - 规则：`allowed` 行 → `decision_state='allowed'`、`source` 取行内 `source`/note 的批次、`version=1`；
     `rejected` 条目 → 无行则插入 `enabled=0`、有行则置 `enabled=0`，同样 `version=1`；
   - **幂等**：已有 `decision_version >= 1` 的行不覆盖（重复执行结果相同）；
   - 输出**校验报告**：逐条列出 `phash / 决定 / 来源 / version`，以及"JSON 有但库无"的差集（应为空）。
3. **启动门禁的既有事实**：应用启动时会比对库 revision 与代码 head，**库落后就拒绝启动**
   （`tests/test_r132_review_unmigrated_startup_boundary.py` 已证明：`check.returncode != 0`
   且 stderr 同时列出 `c9a1f4d27e30` 与 `d4b7c1e9a502`）。因此：
   **新代码上线必须与迁移同一窗口**（§11 问题 5 请您确认是否接受这一耦合）。

---

## 6. 兼容矩阵

| 代码 \ 库 | 旧库（无新列） | 新库（已迁移，未回填） | 新库（已回填） |
| --- | --- | --- | --- |
| **旧代码** | 现状 | 忽略新列，读 JSON（保持最新）→ 行为不变 | 同左 |
| **新代码** | 启动门禁拦截（既有行为）；CLI 工具**显式失败**：`AUTHORITY_UNAVAILABLE`（绝不把"列不存在"当成"没有撤回记录"） | 工具区分 `decision_state=''` → 报 **`AUTHORITY_NOT_BACKFILLED`** 并按"证据不足"处理（fail-closed） | 全链正常：库为权威，JSON 为导出 |

要点：**未回填 ≠ 没有记录**；任何"读不到权威"的情形都必须显式报告，不得静默降级。

---

## 7. 备份与回滚

**迁移前备份（同一窗口内）**：

```
data/moderation.db                      → 备份副本（复用 scripts 里已有的 backup_sqlite 思路）
data/moderation.db.rejections.json      → 备份副本
docs/evidence/image-review/batch-*      → 批次目录（清单/结论/原图）+ sha256 清单
```
并把三者的 sha256 记入迁移记录（与第十四批"审计先落盘再提交"的口径一致）。

**回滚 R1（推荐，针对代码问题）**：
1. 先用新代码 `--export-only` 把 JSON 与库对齐（旧代码依赖 JSON）；
2. `git revert` 新代码提交（**不动 schema**，多出的列被旧代码忽略，无害）；
3. 复跑判定/白名单相关回归 + 只读身份报告。

**回滚 R2（针对 schema 问题，可选）**：`alembic downgrade -1`（`batch_alter_table` 去列）。
但我们建议**默认保留列**：SQLite 去列需要重建表，风险大于收益。

**演练**：在**合成库**上完整走一遍「备份 → 迁移 → 回填 → 导出 → 回滚 R1 → 再启动 + 对账」，
留日志与对账报告；**不在生产演练**（需负责人窗口另行安排）。

---

## 8. 测试计划

**新增探针（拟定 5 类）**：

| 类别 | 断言 |
| --- | --- |
| 导出失败 | 决定已提交（库内 state/version 正确）+ `DECISIONS_COMMITTED_EXPORT_PENDING` + 非零 + **不回滚**；`--export-only` 后 JSON == 库 |
| 提交后崩溃 | 进程在导出前被终止 → 重启后 `--export-only` 使 JSON == 库；身份工具显式报"导出陈旧" |
| 并发两决定 | 撤回→放行 与 放行→撤回 两序：`decision_version` 单调、最终 JSON == 库、**无分歧**（不需要锁也成立） |
| 未迁移/未回填 | 旧库/空 `decision_state` → 工具显式失败并给出原因，绝不报"无撤回记录" |
| 回填幂等 | 连跑两次 `--apply`：行数、state、version 不变；差集为空 |

**语义变更清单（请您逐条确认；我方不会先改）**：

| 现有用例（您的原件） | 现状断言 | A2 下建议的新语义 |
| --- | --- | --- |
| round11 `test_partial_snapshot_reports_accepted_manual_recovery_state[fail_second=True]` | 第二张快照失败 → 回滚第一张，输出 `COMPENSATION_CONFLICT/conflicts=1` | **两张都生效**（决定已提交）+ 导出待重试 + 非零；不再回滚 |
| round12 `test_c03_second_snapshot_commit`（本批已按方案 B 适配） | 阻塞 + 顺序完成 + 最终一致 | 该反例**整类消失**；改为"导出待重试 + 重试后 JSON == 库" |
| `tests/test_r132_review_compensation_ownership.py`（5 项） | 补偿不撤销外部/后续决定 | 不再有补偿；改为"单一权威 + 版本单调 + 导出一致" |
| r9 `test_concurrent_reject_and_reapprove_share_one_authoritative_state` | 两次成功操作不得同时 enabled 且 rejected | 同不变式，改为直接对"权威状态"断言（不再依赖锁交错） |

---

## 9. 与您三条关闭标准的映射

| 您的标准 | 本提案如何满足 |
| --- | --- |
| 审核发布与补偿决策/提交处于共同边界 | 权威收进**一个事务**；导出在事务外且**不需要**与判断共享边界（失败即可重试，且不回滚） |
| 首选：决定/source/操作版本/enabled 同库事务保存，JSON 仅作可重试导出 | 就是本提案本身（§3/§4） |
| 若继续不增表：所有路径共用跨进程同步且覆盖全链 | 本批（方案 B）已实现并通过；A2 是其**替代**而非叠加：A2 下 `decision_lock` 仅用于导出顺序 |

---

## 10. 影响面 / 不做什么

**会改**：`app/models.py`（+5 列）、一个 alembic 迁移、`scripts/apply_review_decisions.py`（去补偿）、
`scripts/image_allowlist_seed.py`（权威读写 + 导出）、`scripts/verify_approved_identity.py`（改读库）、
`scripts/image_review_export.py` / `image_allowlist_replay.py`（候选来源改读库）、+2 脚本（回填/对账）。

**不会改**：判定逻辑与阈值、`enforce` 开关状态、群授权/路由、生产动作开关、
运行时白名单读取路径（`image_hash.py`）、任何现有证据原件与批次目录。

---

## 11. 请您确认的 6 个问题

1. **表形态**：A2（扩展 `image_allowlist`，推荐）还是 A1（新建 `image_review_decisions`）？
2. **rejected 必须落行**（`enabled=0` + `decision_state='rejected'`）是否接受？它会改变"`image_allowlist` 只存放行行"的现有语义，但换来唯一权威；运行时读取不受影响。
3. **导出失败的语义变更**：`DECISIONS_COMMITTED_EXPORT_PENDING` + 非零、**不回滚**；
   相应地，§8 里您三个原件中编码"补偿恢复"的用例要按新语义重新定义——**是否确认**？
4. **回填方式**：迁移只做 DDL，回填用独立脚本（默认 `--dry-run` + 校验报告 + 幂等）是否可接受？
   还是要求回填必须在迁移内完成？
5. **迁移与部署同窗口**：启动门禁会拒绝"库 revision < 代码 head"，所以新代码必须与迁移同时上线。
   您是否接受这一耦合？如需解耦，请给出您认可的形态（例如先上"忽略新列"的兼容版本）。
6. **回滚口径**：R1（先 `--export-only` 对齐 JSON → revert 代码 → 保留新列）是否可接受？
   是否需要在提案里补充 R2（`batch_alter_table` 去列）的完整脚本？

---

## 12. 边界声明

本提案**未实现任何代码、未新增迁移文件、未执行任何迁移或部署、未改动生产库/开关/授权**。
所有分析基于只读检查与既有证据；生产运行态仍是"67 群可路由 / `recall_only` / `IMAGE_HASH_MODE=shadow`"（送审方声明）。
等您对 §11 的裁定后，我再按批拆分为可审查的代码 + 迁移 + 回填 + 测试材料。
