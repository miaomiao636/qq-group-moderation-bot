# r132 主审意见整改报告（2026-09-18 晚）

> 对象：主审包 `qqbot-review-pr45-84a8b47`（PR #45 / 受审 SHA `84a8b47`）。
> 本报告按主审模板组织：**逐项给出修改位置、探针前后结果、正式回归、剩余风险**，
> 并单列**负责人决策项**（未获明确授权的不得写成"已确认"）。
> 独立复核结论见 `docs/2026-09-18-r132-reviewer-verification.md`。

## 0. 结论摘要

| 项 | 结果 |
| --- | --- |
| 主审探针（原样入库为正式回归） | 整改前 **12 failed / 30 passed** → 整改后 **42 passed**（未改动任何断言） |
| 专项回归（8 文件） | 配对 6 文件 + 名单 + 小程序码 **全部通过** |
| 全量测试 | **1418 passed / 15 skipped / 0 failed**（本机 Windows） |
| 静态门禁 | `ruff check` + `ruff format --check`（238 文件）、`mypy`（88 源文件）通过 |
| 迁移 | 升 → `alembic check` → 降 → 再升，往返在**独立临时库**通过（主审探针 + 既有迁移探针） |
| 提示词版本 | `t204-v15` → **`t204-v16`**（`.env` / `app/config.py` 默认值 / `.env.example` **三处对齐**） |
| 部署 | **未部署**（整改待负责人授权后重启加载；生产当前为 17:18 加载的 `t204-v15`） |

## 1. 逐项整改（F01–F10）

### F01（P1）窗口豁免只看主类别，漏过同条消息其他图片的色情/暴力

- **修改**：`app/moderation/wall_pair.py`
  - 新增 `effective_categories(decision)`：取**主类别 + 全部规则命中类别**；
  - `is_pairing_candidate` 与豁免判定都改用该集合，命中 `{porn, violence, flood}` 即**不豁免**。
- **探针**：`test_window_evidence.py::test_picture_window_must_preserve_any_porn_signal[2 例]` 改前 failed → 改后 passed。
- **正式回归**：`tests/test_wall_pair.py::test_pairing_blocked_when_secondary_evidence_is_severe`（porn/violence/flood 三例）。
- **剩余风险**：判据基于"已产生的类别证据"；**未产生的**（模型未返回）无从判断——与既有口径一致。

### F02（P1）一张带小程序码的图可替另一张未完成审核的图放行

- **修改**：`app/moderation/ai.py::_miniprogram_qr_allow` 增加前置条件——
  任一 `vision`/`degraded` 结果 `needs_review` 或 `degraded_reason` → **不授予放行**（落回 record_only）。
  只约束附件通道，不牵连文字通道（图带码 + 有文字的普通消息不被无谓拦下）。
- **探针**：`test_r132_qr_unresolved.py` 2 例（unresolved / timeout）改前 failed → 改后 passed；QR+正常图仍放行、明确 porn 控制仍拦。
- **剩余风险**：仍以"模型结构化字段"为触发条件，识别漏判方向保守（漏判 → 不放行）。

### F03（P1）普通新闻/音乐卡片的文案会被认成群卡硬证据

- **修改**：`app/adapters/onebot/parser.py::_is_group_card` **删除 desc/prompt 子串兜底**，只认结构化信号
  （`meta.group` / `app|appName` 含 group|qun / `view == "group"`）；拿不到可靠结构时**保持 False**。
- **探针**：`test_r132_structure_probe.py` 2 例（新闻卡、音乐卡）改前 failed → 改后 passed；纯结构化群卡正例仍命中。
- **剩余风险**：**可能漏撤**部分真实群名片（结构化信号缺失的客户端）——方向是"宁少撤不误撤"；
  需负责人提供真实 NapCat 群名片样本后按 schema 收窄/扩充（NEXT_TASKS 已列）。

### F04（P1）群卡前加一段普通文字，即绕过结构性撤回

- **修改**：`app/moderation/rules.py` 卡片判定由 `msg.kind == "share_card"` 改为
  `msg.kind == "share_card" or msg.share_card is not None`（按**卡片结构**判定，覆盖 text+卡 / image+卡 / 多卡）。
  **不是**"所有 mixed 都撤回"——只有确实含卡片结构的消息才走卡片分支；R006（未知来源卡片）同步修复。
- **探针**：`test_r132_structure_probe.py::test_group_card_structural_rule_survives_mixed_text[with-text]` 改前 failed → 改后 passed；
  `[bare]` 与保护角色控制组不变。
- **剩余风险**：D-032（群主/管理员卡片完全放行）现在也覆盖混合消息——与既有口径一致，已在报告中标注。

### F05（P1）名单导入确认未绑定预览

- **修改**：`app/web/routes.py` + `app/web/agent_confirm.py`
  - 预览阶段用**既有计划设施**创建服务端计划：`create_confirmation(action="allowlist_members_sync", params=文件摘要+完整差异, expected_state=…, requestor=human:…)`
    （`HIGH_RISK_ACTIONS` 新增该动作）；
  - 预览页回传隐藏 `plan_id`；确认阶段 `approve_confirmation`（人工批准）→ `claim_confirmation`
    （**一次性认领**：校验 requestor / action / **服务端重算的差异指纹**）→ 通过后才 `apply_member_import`；
  - 未带 `plan_id`（未预览 / 页面过期）→ 拒绝；差异变化（名单被别人改动、文件被换）→ 拒绝并要求重新预览；
    大幅停用的 `ack` 勾选也绑定同一计划。
- **探针**：`test_member_import_review.py` 2 例改前 failed → 改后 passed。
- **正式回归**：`tests/test_r116_member_allowlist.py` 新增"未预览不得写库""旧预览不得恢复已删除成员""计划一次性"三段断言。
- **剩余风险**：计划 TTL 沿用项目既有 **300 秒**；超时需重新预览（页面已明示）。

### F06（P1）已提交版本不能按现有手册有效回滚

- **修改**：`docs/deploy-runbook-d037-d038.md` §3 **按已提交/已部署版本重写**：
  切版本统一用 `git switch --detach <已验收 SHA>`；明确**`downgrade` 会 DROP 整张 `allowlist_members` 表**（**可逆 schema，非无损恢复**）；
  给出"先导出名单 → 回滚 → 重部署 → 重新导入"的完整链路；写明"**保留名单**与**回退到旧代码**无法同时满足"；
  指明"只关个别行为"应走运行期开关而非版本回退；初版中依赖 `stash`/`checkout --` 的段落标注为**历史记录**。
- **验证**：迁移往返在**独立临时库**通过（`test_r132_member_import_review.py::test_member_migration_downgrade_preserves_existing_tables`：
  旧表哨兵数据保留、降级后新表消失、再升级为空表）。生产演练待负责人授权与维护窗口。

### F07（P2）D-037"不转人工"仍有旁路

- **修改**：`app/runtime/pipeline.py` 对**成员白名单身份**抑制三类"内容不可判定"信号：
  未知消息段（T-306）、媒体缺失/下载失败、语音/视频/文件审核未完成；**配置/一致性异常（如同群多 provider 歧义）仍照常记录**
  （按主审"不能把全部 evidence_veto 一概关闭"的要求做精确区分）。
- **探针**：`test_r132_structure_probe.py` 2 例（unknown-segment / missing-image）改前 failed → 改后 passed。

### F08（P2，基线已有）R005 刷屏可被 ad 主类别遮蔽

- **修改**：与 F01 共用 `effective_categories`——刷屏命中不再被窗口豁免。
  **未改动** `rules.py` 的 `category = category or "flood"` 主类别选择（改它会变更落库类别与统计口径，属负责人决策范围）。
- **探针**：`test_window_evidence.py::test_window_must_preserve_real_flood_hit` 改前 failed → 改后 passed。
- **文档/代码不一致（一并修正记录）**：`DECISIONS.md` 写"1 分钟 >5 条"，代码为
  `FLOOD_MAX_MESSAGES = 3`（60 秒内 ≥3 条）。**本次不改行为阈值**，登记为待负责人确认的口径项。

### F09（P2）提示词与版本标识不一致

- **F09a**：`config/ai_prompt_rules.txt` 严重类别总则新增**【例外优先级】**段（小程序码 / 群主管理员卡片 / 办证三类
  明确覆盖总则）；小程序码条目补"**本条优先于上方总则**"。
- **F09b**：`app/moderation/ai.py` `PROMPT_VERSION` = `t204-v16`；`app/config.py` 默认值 `t204-v6` → `t204-v16`；
  `.env.example` `t204-v4` → `t204-v16`；`.env` 同步（**三处对齐**）。
- **F09c**：`app/moderation/decision.py` 注释更正（"严重类别（诈骗/色情/暴力）"→"**色情/暴力**，诈骗不再例外"）。
- **F09d**：CRLF/LF 导致的 SHA256 差异——认可主审"不据此误报内容不一致"的判断；本报告中的摘要均注明来源与行尾。

### F10（P2）状态与统计需补可核验证据

- **状态文档**：`PROJECT_CONTEXT.md` 改为**带"截至时间"的当前态**（2026-09-18 18:00 本地），
  明确 Git SHA / schema head / 提示词版本三者的区别；旧阶段标为"历史，勿当现行状态"。
  `NEXT_TASKS.md` 同步（送审链、D-037/D-038 部署状态、D-039 版本与例外、急停解除）。
- **证据格式（本报告起统一）**：**Git SHA** = `84a8b47`；**schema head** = `c9a1f4d27e30`；
  **提示词版本** = `t204-v16`（部署前生效值 `t204-v15`），提示词摘要 SHA256 见 §3。
- **可重算导出**：本次不复用已删除的临时脚本；覆盖性结论以**入库探针**（可重跑）为准，
  运行方式与主审 README 一致（`-c pyproject.toml -p conftest`）。
- **待补（如实登记，未宣称完成）**：固定 UTC 半开时间窗 + 账号 + 群集合 + 部署 SHA 绑定的动作统计导出；
  `code 1200` 仅能证明"调用超时"，不能推断 QQ 侧未执行；两份证据需在**重新送审前**补入。

## 2. 已保留的控制（未被本次整改放宽）

1. D-037 成员白名单仍是"全类别完全放行"（负责人明确选择），本次只**收紧**其被降级/被旁路的路径。
2. D-038 合并转发/群名片仍"一律撤回"（保护角色与成员白名单除外）——本次修复的是**绕道**，不是放宽。
3. R09 前缀语义、同秒不豁免、未完成图"不处罚也不豁免"、账号/群/成员作用域隔离**均未改动**。
4. `recall_only` 阶段、授权群边界、永不产生踢人动作**均未改动**。

## 3. 部署前需要的信息（重新送审时一并给出）

| 项 | 值 |
| --- | --- |
| Git SHA | **`0a6c92d`**（整改提交；其上 `a1514db` 为合并 `origin/main` 的合并提交，已解除 PR 的 CONFLICTING 状态） |
| 比较基线 | `main@68a94b9`（r127 P1 关闭） |
| schema head | `c9a1f4d27e30`（**未新增迁移**） |
| 提示词版本 | `t204-v16`（`.env` / `app/config.py` 默认值 / `.env.example` 三处对齐） |
| 提示词摘要 | `config/ai_prompt_rules.txt` SHA256（**工作副本 CRLF 形态**）= `21984EFF4F1A1AFFF8B175E0FE60D1733A0CDAC7362C38409751AC78F2F5F167`。按主审 F09d：仓库 LF 形态摘要不同，**不据此判定内容不一致** |
| 门禁 | 全量 **1418 passed / 15 skipped / 0 failed**；`ruff check` + `ruff format --check`（238 文件）；`mypy`（88 源文件） |
| 部署状态 | **未部署**（生产仍为 `t204-v15` + 受审代码；待负责人授权重启） |

## 4. 负责人决策项（主审 §5，本报告不代决策）

| # | 项 | 本次处理 |
| --- | --- | --- |
| 1 | 窗口内**诈骗**是否豁免 | **按负责人 2026-09-18 第三条口径实施：仅当来源图为"带小程序码的放行图"时豁免诈骗**；校园墙来源图之后的诈骗照常处理（回归 `test_pairing_does_not_exempt_fraud_after_campus_wall_source`） |
| 2 | 窗口内**刷屏**是否豁免 | **保持不豁免**，并以 F01/F08 修复保证"不豁免"真正生效（此前会被广告主类别遮蔽） |
| 3 | 图片哈希白名单 | 未实现（缺样本）；需负责人提供"确定该放行"的原图 |
| 4 | 单模型直接决定 85% 图片处罚 | 未改动（属负责人决策） |
| 5 | 名单大幅停用阈值（5 条且 30%） | 未改动（F05 的计划绑定不依赖该阈值） |
| 6 | D-038 动作等级 | 仍为建议 recall/mute/warn、执行层 `recall_only`；未提高禁言/警告等级 |
