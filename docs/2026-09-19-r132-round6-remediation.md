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
| **R09-D** | 来源资格只看 `source=="vision"`，**降级附件（超时/限流/读取失败 → `source="degraded"`）被整个丢掉** → "一张未决附件 + 一张带码图"的消息被当成已确认来源，窗口内豁免后续诈骗 | `_confirmed_sources` 改为对 **`vision` + `degraded` 全部附件**做资格校验：任一附件降级/需人工/类别非空/降级原因非空 → **整条消息不作来源**（与"在途既不处罚也不豁免"同一语义） | `test_r132_review_*` / `previous/test_new_shadow_boundaries.py` |
| **A06-R** | 在线/离线两套判据：在线只看顶层类别与少数规则，离线另写一套 | 抽出**同一** `detail_blockers`，在线 `merge_ai_evidence` 与导出/回放**共用**；并把本服务**实际配置的三个阈值**（direct/low/high）透传到附件复核判据 | `previous/core`、`previous/scripts` |

### 名单同步类

| 编号 | 根因 | 修法 | 对应回归 |
| --- | --- | --- | --- |
| **N-F05-2-R** | 版本集合只含"被改动行"（`row_versions` 只收 touched）→ 文件内**未变动成员**被删除/停用/改备注后，计划仍报 APPLIED | `row_versions` 覆盖**文件内全部成员**（含预览时 unchanged）；执行写锁内按**完整状态**复核，任一漂移即拒绝旧计划并要求重新预览 | `previous/stats`、`tests/test_r132_f05_round2_edges.py` |

### 部署 / 证据工具类

| 编号 | 根因 | 修法 | 对应回归 |
| --- | --- | --- | --- |
| **F06-A** | 回滚脚本 `from app.core.config import get_settings`——**该模块不存在**（实为 `app.config`），且默认入口正好走这条死路 | 改 `app.config`，并**实跑默认 CLI 入口**验证 | `scripts/*`（`test_image_tool_set_consistency` 同族门禁） |
| **F06-B** | 切码后仍依赖**目标旧树里不存在**的文件（`scripts/rollback_preflight.py`、`app/moderation/allowlist_members_io.py` 在目标 SHA 均不存在） | 版本核对改为**不依赖目标树文件**（纯 stdlib 读 `alembic_version`）；降级前完成检查，切码后只做 stdlib 核对 | 同上 |
| **F06-C** | 启动段 `sc.exe start … \| Out-Null` 后直接打印完成，**不查退出码** | 每个服务单独查退出码 + 以服务状态/healthz 决定是否打印完成，失败**不到达**打印完成 | 同上 |
| **A09** | 统计窗口起点用 17:00:00，含部署前 82 秒 | 默认起点改为**已证明生效的时刻** 17:01:22 | `previous/stats/test_window_stats_review.py` |
| **A03-R** | `off` 模式下不得做任何观察 I/O | 观察分支前置 `mode()` 判定，`off` 直接返回 `None` | `probes/test_mode_pipeline_controls.py` |
| **A06（工具）** | 离线导出/回放只看顶层类别 | 与在线**同源**看完整证据（严重类别 / 未定论 / `evidence_vetoes`） | `previous/scripts/test_image_tools_contract.py` |

### 图片哈希 / 集合一致性类

| 编号 | 根因 | 修法 | 对应回归 |
| --- | --- | --- | --- |
| **A04** | 导出按"命中种子"分组，把**字节不同但哈希相近**的图折叠成一条、只展示第一张 | 改为按 **(命中种子, 文件 SHA-256)** 分组；清单新增 **文件 SHA-256 / 文件 dHash / 命中种子 dHash / 距离** 四列，文件名用该文件自己的 dHash+SHA 前缀 | `test_r132_review_image_tool_set_consistency.py` |
| **A05-R / 集合语义** | `values or None` 把"**表存在但为空**"与"**表缺失**"混为一谈 → 已被停用/排除的图被重新当候选报出 | 空集是结论、`None` 才是"读不到"；生效名单存在时**只认生效名单** | 同上（3 组 parametrize 共 6 项） |
| **G02 → 已更正** | 我此前把动图**整个排除**出哈希，会把负责人认可的首帧画面漏掉 | **按首帧参与**（PIL 默认停在第 0 帧）；新增 `frame_scope_of()`，命中多帧图时标 `frame_scope="first_frame"`，**明确标注"只代表首帧、不声称整图等价"** | `previous/core/test_image_hash_core_review.py` |
| **缺表 ≠ 干净未命中** | `load_enabled` 吞异常返回 `[]`，把"读不到"写成"查过了没命中"——**零证据给出结论** | 新增 `load_enabled_or_none`：表缺失/查询失败 → `None` → 观察侧标 `unavailable="db_failed"`；`load_enabled` 退化为它的 fail-closed 包装（判定路径口径不变） | `shadow/test_shadow_remaining_contracts.py` |
| **导出空名单塌陷** | 表存在但为空时导出"0 张图"并**以成功退出**，等于谎报"没有需要复核的图" | 生效名单为空时审核批次 = **"判定里确实出现过的图" ∩ 样本库**（未停用/排除）：既不静默输出空批次，也不把无判定引用的样本、或历史放行图混进来 | `test_r132_review_image_tool_set_consistency.py` |
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
- **本仓库全量**：`1697` 用例 / **0 failed / 0 error / 15 skipped**（环境相关跳过）。
- **静态门禁**：`ruff check` + `ruff format --check`（275 文件）、`mypy app`（89 源文件）全绿。
- **同 head CI（三 job）**：
  - 代码提交 `22457f1`：[run 35431357177](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35431357177)
    —— `Lint, Type-check & Test (ubuntu-latest)` / `Lint, Type-check & Test (windows-latest)` /
    `Runtime deps regression (clean install)` **三个 job 全部 success**；
  - 探针入库提交 `89865e1`：[run 35431717989](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35431717989)
    —— Ubuntu `jobId=105867402227` success、clean runtime-deps `jobId=105867402303` success，
    Windows `jobId=105867402292`（在写本文时仍在跑，结论见 PR 页面 / 后续提交）。

---

## 三、如实登记的缺口（未糊过去）

1. **未部署**：生产仍是先前加载的代码，本批全部改动**未重启、未加载**；`IMAGE_HASH_MODE=off`
   （默认）未变，**图片哈希相关改动对线上零影响**；`image_allowlist` 迁移**未执行**；`enforce` **未实现**。
2. **Windows 回滚链未实机演练**：只有静态门禁 + 隔离库逻辑测试（`tests/test_r132_rollback_preflight.py`，25 项），
   **没有**停过生产服务、未在生产库演练。
3. **图片哈希白名单尚未建立**：仍缺负责人确认"确定该放行"的原图；上线前需先用真实样本校准阈值。
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
4. **导出审核范围**："生效名单为空时 = 判定出现过的图 ∩ 样本库"是否与"整表同步 / 期间变化需重新预览"的
   既有承诺冲突。
5. **动图首帧口径**：`frame_scope="first_frame"` 的标注是否足以让审核者知道"命中 ≠ 整图等价"。
