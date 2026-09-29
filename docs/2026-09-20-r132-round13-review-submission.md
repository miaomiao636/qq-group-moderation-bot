# r132 第十三批送审材料

日期：2026-09-20。**基线**：`6505a79`（您第十一批复验的受审版本）。本批 = 您 C03-R / C04-R / C05-R / T01 整改 + 我自己的口径更正。

| 提交 | SHA | 内容 |
| --- | --- | --- |
| **代码修复** | **`228f276`** | C03-R1/R2、C04-R、C05-R（只动 `scripts/` 四个文件） |
| **测试入库** | **`2f69fdf`** | 原 31 项包 + 本轮 23 项业务探针 + stale-true 输入适配（只动 `tests/`） |
| 文档/证据 | **`3252f7d`** | 本材料 + 纯文本回评 + 只读身份报告 + 第十二批口径更正 |

---

## 一、先认 T01：我上一批的不实宣称（已原地更正）

我在第十二批材料 §一 写的两句**不成立**：

- "您本轮 31 项探针**先原样入库**"——**没有入库**。`git diff --name-status 194eb0b 6505a79` 只改了 2 个既有测试文件（`tests/test_migration_notifications.py`、`tests/test_r132_review_group_enable_boundaries.py`），**没有任何新探针文件**；仓库 reviewer 实为 **196**。
- "全部入库 reviewer 探针 **227 passed（196 + 31）**"——**不成立**。我当时是在**外部目录**用 `-p conftest` 跑出了 31 passed，却把它写成了"已入库、并被同 SHA CI 覆盖"。

这是**证据边界**错误，不是笔误：我把"本机跑过"当成了"入库 + CI 覆盖"。第十二批材料里那两行已**原地加【更正】**，本批**真正入库**（见第三节），并给出 Git 收集清单与 AST 一致性证据。

---

## 二、证据塔（本批）

| 检查 | 结果 |
| --- | --- |
| 主审原 31 项（`previous31/` 三个原文件） | **31 passed**；本批**真正入库**为 3 个测试文件 |
| 主审本轮 24 项（23 业务 + 1 AST 审计） | 入库时本机复现 **13 failed / 11 passed** → 整改后 **24 passed**；【校准 2026-09-20】该 24 passed 的执行版本是 **`228f276`（仅代码修复阶段）**；在最终 HEAD 上正确口径是 **23 项业务项通过 + 1 项历史 AST 审计"不适用"**（stale-true 修正后该历史审计不再相等，按主审意见登记、不入永久套件） |
| 入库一致性 | 6 个新入库文件与您的原件 **AST 逐节点一致**（`ruff format` 只规范空白/引号风格；唯一差异 = 按您要求未入库的那 1 项 AST 审计函数） |
| 仓库 reviewer 收集数 | **250**（196 + 31 + 23）；本批再增 12 项 → **262** |
| 仓库全量（本机） | **1882 用例 / 0 failed / 0 error / 15 skipped**（本批后 **1895**） |
| 静态 | `ruff check` ✓、`ruff format --check` ✓（**304 文件**，【校准】原写 298 是入库 6 个测试文件之前的数）、`mypy app` ✓（89 文件） |
| C03 子包 | 您 C03 新 4 项 **4 passed**（2 个原失败反例 + 2 个正控） |
| 生产身份链（**修复后**只读复跑） | `IDENTITY_OK allowed=68 (mismatch 0) rejected=1 (mismatch 0) snapshot_state=ok` → `docs/evidence/image-review/identity-20260920T053215Z.{md,json}` |
| **同 SHA CI（`3252f7d`，含本批全部代码+测试+文档）** | [run 35492179873](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35492179873)，attempt 1：`ubuntu-latest` `jobId=106028899903`、`windows-latest` `jobId=106028899883`、`clean runtime-deps` `jobId=106028899737` —— **三 job 全部 success** |

---

## 三、T01：真正入库（含收集清单与一致性证明）

**新增 6 个文件**（`tests/`）：

| 文件 | 来源 | 用例数 |
| --- | --- | --- |
| `test_r132_review_compensation_ownership.py` | `previous31/test_compensation_ownership.py` | 5 |
| `test_r132_review_group_authorization_round11_edges.py` | `previous31/test_group_authorization_round11_edges.py` | 12 |
| `test_r132_review_identity_remaining_and_utc.py` | `previous31/test_identity_remaining_and_utc.py` | 14 |
| `test_r132_review_round11_group_closure.py` | 本包 `probes/groups/test_group_c01_c02_closure.py` | 5 |
| `test_r132_review_round11_c03_operation_identity.py` | 本包 `probes/images/test_c03_operation_identity.py` | 4 |
| `test_r132_review_round11_identity_collisions.py` | 本包 `probes/evidence/test_identity_collisions_and_utc_precision.py` | 14 |

合计 **54 项新增**（31 + 23 业务反例/正控），仓库 reviewer 由 196 → **250**。

**逐处登记（全部为输入/格式适配，无业务断言改动）**：

1. 每个文件**只加文件头注释**（`# ruff: noqa: ...` + 来源与适配说明），
2. `ruff format` 规范化**空白/引号风格**（AST 复比逐节点一致，见下），
3. **唯一功能差异**：`test_group_c01_c02_closure.py` 里的
   `test_existing_boundary_business_test_asts_are_unchanged` **未入库**——
   按您"只用于本 SHA 审计、不作为永久业务测试"的要求；另外它依赖
   `git show 194eb0b:<path>`，depth-1 的 CI checkout 取不到。

**一致性复算**（本机实跑）：对 6 个文件做 AST 逐节点比对（去掉头部注释、并剔除上面那一个函数），结果全部 **AST-IDENTICAL**。

---

## 四、C03-R：两条反例的修法（在现有补偿方案内，未新增表/迁移）

### R1 归属凭据（写事务内产出）

`import_seeds(..., write_credential=...)` 在**同一个写事务内**回填每个涉及哈希
「写入后的完整行状态」（`(id, enabled, note)` / 行不存在 → `None`）；补偿改比这份
**凭据**，不再比较"提交后另起只读连接读到的 `after`"。

- 您的反例 A：真 import 提交后、外部连接 DELETE + INSERT(9001,'independent-row')。
  现在 `credential=(原 id,1,note)` vs 现值 `(9001,1,'independent-row')` → **判为不属于本操作
  → 不撤销、保留外部行并报 `COMPENSATION_CONFLICT`**（原实现会删掉 9001 并报 `conflicts=0`）。

### R2 提交前乐观校验（决策依据漂移即放弃恢复）

补偿在 `BEGIN IMMEDIATE` 内三阶段：

1. 记录**决策依据**：恢复开始前每个哈希的快照决定状态；
2. 按"归属凭据 + 矛盾检测"执行恢复；
3. **提交前再读一次快照状态**：只要有哈希漂移 → `ROLLBACK` 本次恢复，
   报 `COMPENSATION_CONFLICT`，**保留后续结果**。

- 您的反例 B（B 已提交 DB、只剩 JSON 写盘）：补偿读快照时放行 B 完成 JSON 写盘 →
  A 的第二次读取发现 `approved → rejected` 漂移 → 放弃恢复 → 最终 **DB 与 JSON 一致（均"撤回"）**。
- 您指出的关键事实我确认成立：`BEGIN IMMEDIATE` **挡不住**"DB 已提交、只剩 JSON 写盘"的写者，
  所以判定边界必须放在**提交前比对快照态**，而不是依赖 SQL 锁。
- 已接受的部分恢复正控（第二张快照写盘失败 → 第一张保持启用 + `COMPENSATION_CONFLICT` /
  `conflicts=1`、无 `IMPORT_OK`）**保持通过**，未改成"全量原子回滚"。

**关于您答复第 1 条**（"建议决定/source/操作版本/enabled 同库事务权威保存、JSON 仅作可重试导出"）：
本批按您允许的**结果验收口径**在现有补偿方案内修（您的关闭标准也写明"不要求指定数据库迁移
或整体架构"）。若您要该架构，我按**独立提案**单独提交（含备份、兼容、回滚方案），
**本批不新增迁移、不执行任何生产变更**。

---

## 五、C04-R：dHash 只作候选索引（不再首匹配替代身份）

| 场景 | 现在的行为 |
| --- | --- |
| provenance 的批次**就是**被核对批次（`honor_hint=True`） | **必须核对 `(batch,no)` 那一行**；该行缺失 → 报"不得按 dHash 自动改绑"；该行 dHash 与生效行不一致 → 同样报错。覆盖 note 指 `#2`（撤回）与 `#999`（不存在）两例 |
| 来源批次 ≠ provenance 批次（合法重编号/重新批准链） | 先建**候选集合**（该批次内同 dHash 的全部行）；**候选里存在相反决定即报歧义**，不再按行顺序取第一个；全候选结论一致才算身份明确；多候选全放行记 `multiple_approved_identities`（**多个批准身份**，不宣称"唯一原图"） |
| `state` | 改**枚举契约**：只认 `approved` / `rejected`；**缺省键**沿用历史默认 `rejected`（与您"历史缺省可有既定兼容契约"一致）；`null` / `true` / `7` / `""` 一律拒绝——不再出现"同一条目同时给放行与撤回开出 mismatch=0、exit 0" |
| `source` | 用 `findall` 取**全部**批次；出现多个（`review:甲;review:乙`）即报歧义，不取第一段 |

**真实数据不受影响**：修复后只读复跑生产证据链仍是
**68/68 放行 + 1/1 撤回、mismatch=0**（生产快照那 1 条无 `state` 键 → 走兼容默认）。
报告新增 `candidate_identities` / `multiple_approved_identities` 作为**实际多候选诊断**字段，
不改变批准集合，也不据此认定历史上已发生错误批准。

---

## 六、C05-R：保留受支持精度（不新增无关限制）

`_parse_utc` 现在**保留非零小数秒**（写回 `.ffffff`），整秒与空格写法格式不变：

- `[00:00:00.500000, 00:00:01.500000)` → 选中 `.500000 / .750000 / 01.000000`（原实现被静默截成
  `[00:00:00, 00:00:01)`，选中前三条，**计数同样是 3 但集合错了**）；
- `Z` 与等价 `+08:00` 两种写法都正确（您的三例中 2 failed 已转绿）；
- 按您答复第 3 条**保留 ISO 支持**，不加无关日期限制；
- 顺带修正非阻塞文案：窗口模式下那段无条件"不同范围、请用 since/until"的警示改为按
  `windowed` 分支（窗口模式明确"同一 UTC 半开范围、可以相除，但仍不等于业务命中率"）。

---

## 七、T01 附属：stale-true 输入（按您的建议小修）

`tests/test_r132_review_group_enable_boundaries.py::test_stale_survey_enabled_flag_does_not_override_actual_db_state`
原来替换的 `load_survey` **已不在执行路径**（实际快照里仍是默认 `false`），因此没有覆盖它命名的
"快照说已开、DB 实际关闭"前提。现改为**真实输入 seam**：

- 读取夹具 `stats/groups-synthetic.json`，把 `groups[0].action_enabled` 写为 `true` 后 UTF-8 回写；
- 断言**解析器确实读到 `True`**（否则该控制证明不了任何东西）；
- **原结果断言逐字保留**（`enabled(path) == 1`）。

同时把您本包里的正控 `test_stale_true_flag_reaches_actual_snapshot_parser_and_db_still_controls`
**转为正式回归**（已随第一节入库）。**不恢复**生产 `load_survey` 兼容分支。

登记：这是本仓库第 5 处适配（前 4 处见第十二批材料 §二），属**既有差异文件内的输入 seam 适配**，
按您"历史 AST 核对不应阻碍合理修整"的意见执行。

---

## 八、未证明项（照旧，不用本地测试补足）

A09 前 6 秒、历史授权快照（`NOT_PROVEN`）、导出多查询一致性读事务、Windows 回滚全链实机演练、
一次未复现失败、`enforce` 最终批准与阈值校准、B 的实际动作效果。
审计里的 `stage_runtime_proven=false` 只是如实标注，不构成"服务已加载 recall_only"的证据。

---

## 九、边界

未启用 `enforce`、未执行回滚、未修改判定逻辑、未扩大授权到其它 provider、未动生产动作开关；
生产仍为 **67 群可路由 / 阶段 `recall_only`（只撤回）/ `IMAGE_HASH_MODE=shadow`**。
本批的测试只用合成图片与临时 SQLite；身份工具只读打开生产库，产出的是报告文件。
