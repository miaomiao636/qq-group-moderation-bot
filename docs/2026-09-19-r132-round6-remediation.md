# r132 第六轮整改对账（送审稿）— 主审包 `85b0c0b`

**结论：主审完整探针包 65 项，整改后 65/65 通过（`65 passed, 0 failed`），且 8 个探针文件已
「原样入库」成为本仓库正式回归（只有文件头一行 lint 豁免，未改任何断言与逻辑）。**

| 阶段 | 主审包结果 |
| --- | --- |
| 收到该包时（对上受审 SHA `85b0c0b`） | **21 failed / 44 passed** |
| 整改后（本仓库当前 HEAD） | **65 passed / 0 failed** |

复跑方式（与主审同法，`-p conftest`、`PYTHONPATH=.;tests`），两种位置都已验证：
① 直接跑主审包目录；② 跑入库后的 `tests/test_r132_review_*.py`（结果一致）。

---

## 一、逐项对账（根因 → 修法 → 验证）

### 运行期判定类

| 编号 | 根因（我的独立复现结论） | 修法 | 对应回归 |
| --- | --- | --- | --- |
| **R09-D** | 来源资格只看 `source=="vision"`，**降级附件（超时/限流/读取失败 → `source="degraded"`）被整个丢掉** → "一张未决附件 + 一张带码图"的消息被当成已确认来源，窗口内豁免后续诈骗 | `_confirmed_sources` 改为对 **`vision` + `degraded` 全部附件**做资格校验：任一附件降级/需人工/类别非空/降级原因非空 → **整条消息不作来源**（与"在途既不处罚也不豁免"同一语义） | `tests/test_r132_source_collection_contract.py`、`tests/test_r132_r09_real_unresolved_shapes.py`、`tests/test_r132_review_new_shadow_boundaries.py` |
| **A06-R** | 在线/离线两套判据：在线只看顶层类别与少数规则，离线另写一套 | ⚠️ **更正（主审 R6-01 指出，我上一版此处的说法不实）**：当时**只统一了"严重类别/未定论/veto"这几个粗分类**——在线用的是 `_attachment_reviews_unresolved`（含**二审异类 / 低置信 / 非独立模型 / 孤儿二审**），离线 `detail_blockers` 里**没有**这些；本轮才真正共用：把在线判据抽成 `attachment_reviews_unresolved`，离线 `model_validate` 还原后**调同一个函数** | `tests/test_r132_review_online_offline_pair_consistency.py` |

### 名单同步类

| 编号 | 根因 | 修法 | 对应回归 |
| --- | --- | --- | --- |
| **N-F05-2-R** | 版本集合只含"被改动行"（`row_versions` 只收 touched）→ 文件内**未变动成员**被删除/停用/改备注后，计划仍报 APPLIED | `row_versions` 覆盖**文件内全部成员**（含预览时 unchanged）；执行写锁内按**完整状态**复核，任一漂移即拒绝旧计划并要求重新预览 | `tests/test_r132_f05_round2_edges.py`、`tests/test_r132_f05_round3_state_drift.py`、`tests/test_r132_f05_round4_supported_boundaries.py` |

### 部署 / 证据工具类

| 编号 | 根因 | 修法 | 对应回归 |
| --- | --- | --- | --- |
| **F06-A** | 回滚脚本 `from app.core.config import get_settings`——**该模块不存在**（实为 `app.config`），且默认入口正好走这条死路 | 改 `app.config`，并**实跑默认 CLI 入口**验证 | `tests/test_r132_rollback_preflight.py`、`tests/test_r132_review_image_tools_contract.py` |
| **F06-B** | 切码后仍依赖**目标旧树里不存在**的文件（`scripts/rollback_preflight.py`、`app/moderation/allowlist_members_io.py` 在目标 SHA 均不存在） | 版本核对改为**不依赖目标树文件**（纯 stdlib 读 `alembic_version`）；降级前完成检查，切码后只做 stdlib 核对 | 同上 |
| **F06-C** | 启动段 `sc.exe start … \| Out-Null` 后直接打印完成，**不查退出码** | 每个服务单独查退出码 + **服务 `Running` 检查**后才打印完成，失败**不到达**打印完成。⚠️ **更正**：脚本**不请求 healthz**，所以这只证明"服务被拉起"，**不等于**应用健康/端到端恢复已完成（应用健康另验） | 同上 |
| **A09** | 统计窗口起点用 17:00:00，含部署前 82 秒 | 默认起点改为 **17:01:22**（新进程启动时刻）；**不等于应用已就绪**——同批另有服务 17:01:28 启动，前 6 秒无逐条归属证明，按未证明处理 | `previous/stats/test_window_stats_review.py` |
| **A03-R** | `off` 模式下不得做任何观察 I/O | 观察分支前置 `mode()` 判定，`off` 直接返回 `None` | `probes/test_mode_pipeline_controls.py` |
| **A06（工具）** | 离线导出/回放只看顶层类别 | 与在线**同源**看完整证据（严重类别 / 未定论 / `evidence_vetoes`） | `previous/scripts/test_image_tools_contract.py` |

### 图片哈希 / 集合一致性类

| 编号 | 根因 | 修法 | 对应回归 |
| --- | --- | --- | --- |
| **A04** | 导出按"命中种子"分组，把**字节不同但哈希相近**的图折叠成一条、只展示第一张 | 改为按 **(命中种子, 文件 SHA-256)** 分组；清单新增 **文件 SHA-256 / 文件 dHash / 命中种子 dHash / 距离** 四列，文件名用该文件自己的 dHash+SHA 前缀 | `test_r132_review_image_tool_set_consistency.py` |
| **A05-R / 集合语义** | `values or None` 把"**表存在但为空**"与"**表缺失**"混为一谈 → 已被停用/排除的图被重新当候选报出 | 空集是结论、`None` 才是"读不到"；`build_whitelist` **只认生效名单**（为空也是结论） | `tests/test_r132_review_image_tool_set_consistency.py`、`tests/test_r132_review_image_set_round6_remaining.py` |
| **G02 → 已更正** | 我此前把动图**整个排除**出哈希，会把负责人认可的首帧画面漏掉 | **按首帧参与**（PIL 默认停在第 0 帧）；新增 `frame_scope_of()`，命中多帧图时标 `frame_scope="first_frame"`，**明确标注"只代表首帧、不声称整图等价"** | `previous/core/test_image_hash_core_review.py` |
| **缺表 ≠ 干净未命中** | `load_enabled` 吞异常返回 `[]`，把"读不到"写成"查过了没命中"——**零证据给出结论** | 新增 `load_enabled_or_none`：表缺失/查询失败 → `None` → 观察侧标 `unavailable="db_failed"`；`load_enabled` 退化为它的 fail-closed 包装（判定路径口径不变） | `shadow/test_shadow_remaining_contracts.py` |
| **导出空名单塌陷** | 表存在但为空时导出"0 张图"并**以成功退出**，等于谎报"没有需要复核的图" | ⚠️ **本轮已改**（主审 R6-02 指出上一版的"把引用样本补进集合"是错的）：**生效集合只认生效名单**；候选项改由 `collect` 收集并**显式标"未在生效名单（候选）"**、**不计入"会改变判定"**；清单头写出**集合模式 + 来源构成 + 读取状态**，并删除"确认后即可切 enforce"的错误指引 | `tests/test_r132_review_image_set_round6_remaining.py` |
| **A11** | 尺寸检查在 resize **之后**（等于永不触发） | 检查移到 resize **之前** | `previous/core` |
| **A07** | 观察/enforce 可能回落到基础 API 默认阈值 | 显式使用负责人审核值 `REVIEWED_MAX_DISTANCE=2` | `previous/core` |

### 统计口径类（A08 四项）

| 口径 | 旧写法 | 现写法 |
| --- | --- | --- |
| **授权按 provider** | 只比群号 → 同一群号在 `qq_official` 授权会顺带给 `onebot` 授权 | 授权集合按 **(provider, external_group_id)** 判定 |
| **越界核查看实际目标** | 比"出现过判定的群" | 改查 **`action_logs` 的 `provider:group`**（实际外发目标），越界项**必须为空**；"出现过判定但未开动作"降级为观察面 |
| **self_id 口径** | 报告标一个账号，数字里混着另一个账号的判定 | 所有判定统计限定 `onebot:<self_id>:%`；另一账号消息**单列**计数 |
| **attempts 语义** | `ok=0` 一律写"失败" | `attempts=0` → **"未发送（跳过/拦截）"**；`err_code=1200` → "超时（最终效果未知）"；真失败 → "失败（最终效果未知）" |

**实测（生产库只读导出）**：实际外发动作**越界 = 无**。

---

## 二、入库与门禁

- **主审探针入库**：`tests/test_r132_review_{mode_pipeline_controls, new_shadow_boundaries,
  unmigrated_startup_boundary, image_hash_core, image_tools_contract, window_stats,
  image_tool_set_consistency, shadow_remaining}.py` —— **原样**，仅加一行文件头
  （`# ruff: noqa: E402, I001, F401, F811[, SIM105]`）与一行出处说明，**未改断言、未改逻辑、未删用例**。
- **本仓库全量**：**1719** 用例 / **0 failed / 0 error / 15 skipped**（含本轮新入库的 22 项）。
- **静态门禁**：`ruff check` + `ruff format --check`（275 文件）、`mypy app`（89 源文件）全绿。
- **同 head CI（三 job）**：
  - 代码提交 `22457f1`：[run 35431357177](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35431357177)
    —— `Lint, Type-check & Test (ubuntu-latest)` / `Lint, Type-check & Test (windows-latest)` /
    `Runtime deps regression (clean install)` **三个 job 全部 success**；
  - 探针入库提交 `89865e1`：[run 35431717989](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35431717989)
    —— **三个 job 全部 success**：Ubuntu `jobId=105867402227`、
    Windows `jobId=105867402292`、clean runtime-deps `jobId=105867402303`。
  - 本文档所在提交（纯文档改动）同样触发三 job CI，结论见 PR #45 页面。

---

## 三、如实登记的缺口（未糊过去）

1. **未部署**：生产仍是先前加载的代码，本批全部改动**未重启、未加载**；`IMAGE_HASH_MODE=off`
   （默认）未变，**图片哈希相关改动对线上零影响**；`image_allowlist` 迁移**未执行**；`enforce` **未实现**。
2. **Windows 回滚链未实机演练**：只有静态门禁 + 隔离库逻辑测试（`tests/test_r132_rollback_preflight.py`，25 项），
   **没有**停过生产服务、未在生产库演练。
3. **图片哈希白名单尚未建立**：**最终批准集合 / 校准样本是否齐备及其入库状态仍待确认**——
   桌面上已有若干样本文件与审核记录，但**它们不等于已建立的生效名单**（`image_allowlist` 迁移未执行、无 `enabled=1` 行）；
   本轮的"候选 / 生效"口径修好后，仍需负责人逐张给结论，再谈阈值校准。
4. **动作统计原件**：固定 UTC 半开窗口导出已产出（`docs/evidence/stats/window-*.md`），
   但"绑定账号 / 群集合 / 部署 SHA"的行级原件仍需在部署后用当时 SHA 重出一次。

---

## 四、请主审重点复核

1. **R09-D 的资格口径**：降级附件是否应当让**整条消息**失去来源资格（我的实现），而不是"仅该附件不参与"；
   以及"未决附件 → 整条不作来源"是否与在途保护的既有边界一致。
2. **N-F05-2-R**：`row_versions` 扩大到"文件内全部成员"后，是否仍能通过"期间别人合法加人"而不误伤
   （我的实现：文件外成员的启用状态漂移 → 拒绝并要求重新预览，**不擅自停用**本次未批准的成员）。
3. **集合语义三态**：`表缺失(None)` / `表存在但为空(set())` / `表存在有内容` 的分支是否穷尽、
   有无第四条路径（例如表存在但列缺失）。
4. **导出审核范围**：本轮已按 R6-02 改为"生效集合只认生效名单 + 候选由 `collect` 显式标注"，
   请复核这一分列是否彻底、还有没有别处把候选当生效用。
5. **动图首帧口径**：`frame_scope="first_frame"` 的标注是否足以让审核者知道"命中 ≠ 整图等价"。

---

## 五、第七批整改（对主审 `2ff2a8a` 复验的回应）

主审对 `2ff2a8a` 的结论是"部分通过、不能整批关闭"，新增 22 项探针 **14 failed / 8 passed**。
我独立复现出**逐字一致**的 14 项失败，逐一核实代码后确认**四项 P2 全部属实**
（含我上一版文档里"在线离线已共用同一 `detail_blockers`"的**不实陈述**），并按主审给的最小关闭标准修完：

| 主审编号 | 修法 | 复现→整改后 |
| --- | --- | --- |
| **R6-01** 离线二审未复用在线判据 | 在线判据抽成**唯一**的 `app.moderation.ai.attachment_reviews_unresolved(ai_results, local=None, *, 三阈值)`；离线 `detail_blockers` 用 `AIModerationResult.model_validate` 还原后**调同一函数**；`local=None` 时只用**已持久化**的 `review_reason`，**不重算、不按默认阈值猜"已消疑"**；旧记录缺 `review_role`/`review_group` 且确有复核痕迹 → 标 `unresolved_unknown`（保守计入例外） | 6 failed → 通过 |
| **R6-02** 生效/候选混用 | `build_whitelist` **只认生效名单**（为空也是结论，不再把引用过的样本补回）；候选项交给 `collect` 收集并**显式标"未在生效名单（候选）"**、**不计入"会改变判定"**；清单头新增 **集合模式 / 来源构成 / 读取状态**（`effective_state` 区分 `ok`/`missing_table`/`bad_schema`/`unreadable`）；删掉两处"确认后即可切 enforce"的指引 | 5 failed → 通过 |
| **R6-03** attempts=0 被算作外发越界 | 目标集合加 `attempts > 0`；`attempts=0`（未发送）与 `attempts IS NULL`（无法判定）**单列、不进目标集合**；与结果表共用同一分类；`attempts>0` 的失败/超时**保留**（不为清零而剔除） | 1 failed → 通过 |
| **R6-04** 动作跨账号混合 | 归属改成**可证明才剔除**：同一 provider 存在判定行、其 `external_message_id` 等于该原始 ID、且该判定**属于别的账号** → 剔除；否则（本账号命中，或**找不到判定行 = 无法归属**）**保留并单列** `action_unattributed_rows`。**不套**判定表的 LIKE 前缀（原始 ID 会重复） | 2 failed → 通过 |

**探针入库**：本轮 3 个文件**原样**入库（只加文件头，未改断言）——
`tests/test_r132_review_online_offline_pair_consistency.py`、
`tests/test_r132_review_image_set_round6_remaining.py`、
`tests/test_r132_review_window_scope_followup.py`，仓库内 **22/22 通过**。

**顺带按 4.2/4.3/4.4 校准**：
- `--start` 帮助与本文 A09 行**改掉"已证明生效"的过强措辞**——17:01:22 只是"可证明已加载的最早时点"，
  同批另有服务 17:01:28 启动，**前 6 秒无逐条归属证明，按未证明处理**；
- 越界结论加**授权快照警示**：只与**导出时刻**的授权集合比较，**不证明动作发生时未越权**（按 `NOT_PROVEN`）；
- 更正本文件的**错误映射**（R09-D → `test_r132_source_collection_contract.py` / `test_r132_r09_real_unresolved_shapes.py`；
  成员并发 → 三个 F05 定向文件；F06 → `test_r132_rollback_preflight.py`）；
- 更正 **F06-C** 表述（只做 `Running` 检查、**不请求 healthz**，不等于应用健康/端到端恢复完成）；
- 删除"空集是结论"与"空集补候选"的自相矛盾表述；
- "样本未到"改为"**最终批准集合 / 校准样本是否齐备及入库状态待确认**"。
