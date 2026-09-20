# r132 第十四批送审材料

日期：2026-09-20。**基线**：`166a1ab`（您第十二批复验的受审最终 HEAD）。本批 = 您 **C03-R2-2 / C04-R2** 整改（**方案 B：跨进程串行化**）+ 两处材料校准。

| 提交 | SHA | 内容 |
| --- | --- | --- |
| **代码修复** | 见提交 `fix(r12)` | `decision_lock` 跨进程边界（全链）+ C04-R2 逐候选完整核验 + 报告穿透 |
| **测试** | 见提交 `test(r12)` | 本轮 12 项入库（C04 原件 verbatim 11 + C03 适配 1）+ 3 处调度适配登记 + 跨进程锁回归 |
| **文档/证据** | 本提交 | 本材料 + 纯文本回评 + 只读身份报告 + 第十三批两处校准 |

---

## 一、先改第十三批的两处口径（您 §4 非阻塞校准，都成立）

| 项 | 原写法 | 现状 |
| --- | --- | --- |
| 格式检查文件数 | 298 | **304**（您指出的正确值；298 是入库 6 个测试文件之前的数）→ 本批再增 2 文件，现为 **307** |
| 上轮"24 passed" | 未标执行版本 | 实际执行于 **`228f276`（仅代码修复阶段）**；在最终 HEAD 上正确口径 = **23 项业务项通过 + 1 项历史 AST 审计"不适用"**。我独立复核：`194eb0b` 与最终 HEAD 该文件均为 14 个函数，**只有 `test_stale_survey_enabled_flag_does_not_override_actual_db_state` 的 AST 不同**（即您说的那一处），已登记、不入永久套件 |

---

## 二、证据塔（本批）

| 检查 | 结果 |
| --- | --- |
| 您本轮新 13 项（11 C04 业务 + 2 C03 调度） | 整改前 **9 failed / 4 passed** → C04 相关 **11/11 通过**；C03 两项的**原到达点已不可达**（见第三节，按您授权的口径改为"阻塞后顺序执行且最终正确"） |
| 上一批原件（31+23 = 54 项） | **54 passed / 0 failed** |
| 仓库 reviewer 收集数 | **262**（250 + 11 C04 原件 + 1 C03 适配版） |
| 仓库全量（本机） | **1895 用例 / 0 failed / 0 error / 15 skipped** |
| 静态 | `ruff check` ✓、`ruff format --check` ✓（**307 文件**）、`mypy app` ✓（89 文件） |
| 生产身份链（修复后只读复跑） | `IDENTITY_OK allowed=68 (mismatch 0) rejected=1 (mismatch 0) snapshot_state=ok` → `docs/evidence/image-review/identity-20260920T093858Z.{md,json}` |
| **同 SHA CI（`0827fda`，含本批全部代码+测试+文档）** | [run 35503172408](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35503172408)，attempt 1：`ubuntu-latest` `jobId=106058432652`、`windows-latest` `jobId=106058432439`、`clean runtime-deps` `jobId=106058432539` —— **三 job 全部 success** |

---

## 三、C03-R2-2：方案 B（跨进程串行化边界）

按您"如继续不增表的补偿方案"的第二个选项实现，**未新增表、未加迁移**。

### 机制

`scripts/image_allowlist_seed.py::decision_lock`（新增）：

- **覆盖范围**（您要求的全链）：**前态读取 → DB 变更 → 决定快照发布 → 失败补偿**。
  `apply_review_decisions` 的整段临界区在该锁内；`import_seeds` 作为写入口也在锁内（可重入，锁内再调用不会自锁）。
- **跨进程**：`O_CREAT|O_EXCL` 锁文件 `<db>.review.lock`（POSIX 与 Windows 同样可用），
  **不只挡线程、也不依赖 SQLite 写锁** —— 您指出的正是"BEGIN IMMEDIATE 挡不住已提交 DB、
  只剩 JSON 写盘的写者"。
- **可重入**：同进程同线程嵌套只累加计数（`apply` → `import_seeds`）。
- **超时即显式失败**：等待 `timeout`（默认 30s）后抛 `RuntimeError`，**本次不做任何改动**；
  锁文件超 `stale`（默认 120s）视为崩溃残留并回收。
- **锁顺序**：本锁（外）→ 拒绝快照锁 `_snapshot_lock`（内）；**不得反向获取**（已写进 docstring）。
- **跨进程证据**：新增回归 `tests/test_r132_decision_lock_cross_process.py` ——
  真实子进程持锁时，本进程拿不到锁、超时后抛 `RuntimeError`、**不误删对方持有的锁**，
  对方释放后可正常获取。

### 调度适配（按您"允许登记输入调度适配"）

锁内串行化后，"后一次审核在 A 的补偿窗口里完成"这一到达点**不可达**（这是修复的目的）。
按您给的验收口径 1（"竞争 B 被可靠阻塞，之后顺序执行且最终结果正确"）登记三处适配，
**全部只改调度，不改最终状态/报告断言**：

| 文件 | 适配 |
| --- | --- |
| `tests/test_r132_review_r9_write_residuals.py`（r9 原件） | 原"第二次操作在第一次发布中同时执行"→ 改为"第二次操作被可靠阻塞，待第一次完成后顺序执行"；**最终不变式（不得同时 enabled 且 rejected）逐字保留** |
| `tests/test_r132_review_round11_c03_operation_identity.py`（第十一轮原件） | 原在 A 的补偿读里放行 B → 改为 B 被阻塞；**最终断言（JSON=rejected、DB 行未 enabled）逐字保留** |
| `tests/test_r132_review_round12_c03_second_snapshot_commit.py`（本批原件，2 参数） | 两个参数（在第 1/第 2 次读放行 B）在新机制下同一结局，合并为 1 个场景：断言**阻塞 + 顺序完成 + 最终一致**（`IMPORT_OK`=2）。原文的"必须打印 `COMPENSATION_CONFLICT`"属您验收口径 2，在 A 的窗口不可穿越后不再适用；A 的补偿报告行仍断言 |

**未为跑探针放松锁**。如实记录：在**当前 SHA 上原样跑您本包的 `probes/`** →
`11 passed / 2 failed`（11 项 C04 全过；2 项 C03 因"B 被锁阻塞、原到达点在 5s 等待后不可达"而失败），
这正是上述不可达情形，不是业务回退。

### 为什么不是"再读一次"

您的判断成立：读第 3、第 4 次都关不掉——"读—判定—提交"与"别处的 JSON 发布"没有共同边界。
本批把边界改成**同一把跨进程锁**，补偿不再依赖"读到的字节是否最新"。

---

## 四、C04-R2：每个候选都走完整身份链

`scripts/verify_approved_identity.py`：

- 抽出 **`verify_entry(no)`**：单行的完整链 = 清单行 → 文件名 → SHA-256 等级（12/64）→
  **磁盘实际字节**（SHA-256 前缀比对）→ **实际 dHash**（可解码性）→ `DECISIONS.json` 人工结论；
- 候选集合路径**对每一个候选都调用它**（不再只核 `matching[0]`）；
- **缺结论不再当"一致"**：`decision != expect` 即该行不完整（`None` 不再是豁免）；
- 任一候选缺证据/矛盾 → 报告**具体编号与原因**（`候选 N：…`）并非零；相反决定另有汇总说明
  （"同哈希候选含相反决定（…）→ 不能用行顺序取第一个"）；
- 全部候选**完整且结论一致**才通过；多于一个时记 `multiple_approved_identities`
  （**多个批准身份**，而非"唯一原图"）；
- **穿透到报告**（您的第 4 点）：逐候选结果放在 `item["candidates"]`，
  并同步进 `reapproval_chain`（新增 `candidate_identities` / `multiple_approved_identities` / `candidates`），
  Markdown 逐条结果在多候选时追加"（候选 N 行：编号…）"。

正控保持：**全候选字节与结论都有效**（approved / rejected 两路）与**合法重批换号**均通过；
不存在"同 dHash 一律失败"的收紧。真实数据复核仍是 **68/68 + 1/1、mismatch=0**。

---

## 五、本批入库（12 项）

| 文件 | 来源 | 用例 | 说明 |
| --- | --- | --- | --- |
| `tests/test_r132_review_round12_all_candidate_evidence.py` | 本包 `probes/evidence/test_all_candidate_evidence.py` | 11 | **verbatim**（只加文件头；与原件 AST 逐节点一致） |
| `tests/test_r132_review_round12_c03_second_snapshot_commit.py` | 本包 `probes/images/test_c03_second_snapshot_commit.py` | 1 | **调度适配版**（见第三节表） |
| `tests/test_r132_decision_lock_cross_process.py` | 送审方自建 | 1 | 跨进程锁 + 超时/失败语义回归（非主审交付件） |

reviewer 收集数：**250 → 262**（`tests/test_r132_review_*`）。

---

## 六、未证明项（照旧）

A09 前 6 秒、历史授权快照 `NOT_PROVEN`、导出多查询一致性读事务、Windows 回滚全链实机演练、
一次未复现失败、`enforce` 最终批准与阈值校准、B 的实际动作效果。
`stage_runtime_proven=false` 只是如实标注。

---

## 七、边界

未启用 `enforce`、未执行回滚、未修改判定逻辑、未扩大授权、未动生产动作开关；
生产仍为 **67 群可路由 / `recall_only`（只撤回）/ `IMAGE_HASH_MODE=shadow`**。
本批测试只用合成图片与临时 SQLite；身份工具只读打开生产库并产出报告文件。
