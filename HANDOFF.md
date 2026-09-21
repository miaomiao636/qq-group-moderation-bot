# Agent交接记录

## 最新入口（2026-09-21）

以 `docs/2026-09-21-project-handover.md` 为唯一接手入口。本轮负责人授权 **UI-PAGING-20260921：后台列表分页与群状态数量**，随后追加 **STABILITY-20260921：查找并修复长期运行问题**。分别见 `docs/2026-09-21-admin-list-pagination.md` 与 `docs/2026-09-21-longterm-stability.md` 的执行 SHA、命令、验证与生产加载状态。分页最终文档版 CI 已成功，稳定性改动须看其自己的 run。生产尚未重启，合并加载需要 Web/Runtime 的明确维护授权。

异常账号研究：负责人拒绝名单匹配，已授权先验证手机辅助路线。手机已连接授权，实机能读取资料卡异常提示和对应 QQ 号；群关联仍依赖代理现场观察的连续导航，尚未实现全群自动遍历。抽样范围、执行 SHA、命令及原始证据入口见唯一入口 §7.1；不能把这次读取验证写成批量巡检完成或准确率证明。手机已恢复到群成员列表。下文保留历史记录，不作为本次部署事实。

## 当前交接：2026-09-18（D-037 成员白名单 + D-038 合并转发/群名片一律撤回）

**任务**：落实负责人 2026-09-18 决策——①白名单除词语外支持**按群成员 QQ 号**添加，名单内成员**所有消息都放行**且**优先级最高**，名单用**一份文本文件**维护、上传即整表同步；②**合并转发与群名片一律撤回**，但**群主、管理员、白名单成员不撤回**；③成员白名单**选 A**（不守 B-2 底线，全类别放行）。

**已实现（代码完成，未部署）**：
- 数据层：`app/models.py::AllowlistMember`（provider + external_user_id 唯一、`sqlite_autoincrement`）+ 迁移 `alembic/versions/c9a1f4d27e30_add_allowlist_members.py`（可完整 downgrade）。
- 运行时：`app/moderation/allowlist.py` 新增 `load_allowlist_members` / `match_allowlist_member` / `add_member` / `set_member_enabled` / `delete_member` / `plan_member_import` / `apply_member_import`（每消息 fresh 直读、fail-closed、全程审计）；`app/moderation/allowlist_members_io.py` 纯函数解析/格式化（BOM、注释、`QQ号,备注`、重复与非法行逐行报错）。
- 规则：`app/moderation/rules.py` 判定链改为「成员白名单 → 保护角色 → **合并转发/群名片一律撤回** → 关键词白名单 → 常规」；`decision.py` 新增 `POLICY_ALLOWLIST_MEMBER_ALLOW`（并入 `POLICY_ALLOW_RULE_IDS`）与 `FULL_ALLOW_NO_UPGRADE_RULE_IDS`（DR 合并用的完全放行集合，**刻意不含 D-031 办证**）；`review_gate.py` 把 `R_FORWARD_RECORD` / `R_GROUP_CARD` 计入独立硬证据（**结构性规则编号不得落在内置 `R0xx` 段**）；`pipeline.py` 装载成员白名单并停止把 `forward_record` 计入转人工兜底。
- 解析：`app/core/contracts.py::ShareCardInfo.is_group_card` + `app/adapters/onebot/parser.py` 群名片多信号判定与群名提取。
- 后台：`app/web/routes.py` 白名单页新增成员区块、文件导入预览页、导入/导出/单条增删启停路由（`UploadFile`，`python-multipart` 已是既有依赖）。
- 文档：`DECISIONS.md` **D-037 / D-038**；`PROJECT_CONTEXT.md` 当前阶段与置信度原则；`PROGRESS.md`； `NEXT_TASKS.md`。

**实现后自查（发现并修复 1 个真实漏洞）**：
- **媒体层可旁路成员白名单**：`image_engine.merge_decisions` 在媒体判 `violation_high` 时会把 `allow` 直接改写为 `violation_high`（并给出 recall/mute/warn），AI 合并分支对 `violation_high` 亦直接放行 → 白名单成员发违规图片仍会被**真实撤回/禁言**，与"所有信息都通过"的负责人口径相悖。修复：`pipeline` 在合并媒体违规**之前**判断成员白名单政策标记，命中即只记录不升级（与保护角色同样免罚的待遇一致）；并补对照回归证明**非白名单成员同一张违规图仍照常判违规**（未放松媒体防线）。
- 另修：同一条消息含多张卡片时，若其中任一为群名片则整体标记为群名片（避免被前面的普通卡片掩盖而漏撤）。
- **未发现其他问题**：迁移实跑核对 DDL 为 `id INTEGER PRIMARY KEY AUTOINCREMENT` + `UNIQUE(provider, external_user_id)`，upgrade → downgrade → upgrade 可逆；群主/管理员分支、复核门硬证据、审计、fail-closed 均按预期。

**验证（本机，未部署）**：`uv run pytest` → **1350 passed / 15 skipped / 0 failed**（新增 `tests/test_r116_member_allowlist.py` 30 项）；`uv run ruff check app tests alembic`、`uv run ruff format --check app tests alembic`（231 文件）、`uv run mypy app`（88 源文件）全部通过。为对齐负责人口径，同步更新 `tests/test_moderation_rules.py`（合并转发由"不误判"改为"一律撤回"）与 `tests/test_onebot_ws.py`（合并转发由"降级人工"改为"一律撤回"），并把 `tests/test_r115_admin_migration.py` 的 `NEW` 由硬编码改为 `get_head_revision()`（否则新增迁移必然让 `alembic check` 失败）。

**未验证 / 遗留（不得宣称已通过）**：
1. **已部署并取生效证据（2026-09-18 14:36）**，但本决策**不构成实机验收**：W2/W3/W4/W5 与 24×7 整机验收仍按 `docs/windows-delivery-checklist.md`；**急停状态（本句为当时记录，已被后续事实覆盖）**：14:34 开启 → **2026-09-18 14:49 已由负责人解除**（见下方"负责人解除急停与真实动作复核"），当前真实动作（撤回）按原配置生效。
2. **群名片识别未用真实样本验证**：当前判定基于工程推断的 `com.tencent.qun.share` / `meta.group` 等信号；需抓一条真实群名片（脱敏）确认字段，必要时收窄，并确认是否要覆盖"万能校园墙"等允许来源。
3. **合并转发/群名片的处罚档次**：走标准高置信阶梯（`record_violation` 规划 recall+mute+warn，当前 `recall_only` 阶段线上只撤回）；负责人只要求"撤回"，若要永久"只撤回不入违规阶梯"，需另立政策改处罚阶梯。
4. **初始名单已导入（5 条）**：由负责人 Desktop 的 `白名单.txt` 经"与后台导入同一代码路径"写入并留审计；真实 QQ 号只落在数据库，**未写入仓库任何文件**。后续加减人员仍在同一个文件里改，再由后台「白名单」页上传（预览→确认）。
5. **主审复验未做**：D-037 的"不守 B-2 底线"是高风险政策，建议送主审复验全链路不可升级与审计/回滚边界。

**下一步建议**：①~~观察影子判定后按需解除急停~~（**已于 2026-09-18 14:49 完成**，本条为历史记录，不再是遗留动作）；②抓真实样本（群名片/合并转发/撤回通知各一条，脱敏）→ 收窄群名片识别；③送主审复验 D-037/D-038；④后续加减白名单人员继续用同一份文本文件、经后台「白名单」页上传核对。

**✅ 已于 2026-09-18 14:36 部署生效（负责人授权"直接执行"）**：按 `docs/deploy-runbook-d037-d038.md` 执行——急停开启（审计 `emergency_stop`，UTC 06:34:05）→ 在线备份 `data/backups/moderation-20260918T063411Z-sb9t2a82.db`（50,860,032 字节，`quick_check=ok`，异地副本 `E:\qqbot-backups\`）→ 提权停服 → `alembic upgrade head`（`b8d4f2a05e31 → c9a1f4d27e30`，`alembic check` 零漂移）→ 新表校验（AUTOINCREMENT + 唯一约束）→ 提权启服（两服务 Running）→ healthz `status=ok / onebot=ready / connected=true / queue_backlog=0`、`processed_total=1`（新代码已处理真实消息）→ 导入成员白名单 5 条（`provider=onebot`，审计 `allowlist_members_import`，UTC 06:37:02）→ 引擎级功能校验（白名单成员诈骗文本 `allow`+政策标记、白名单成员合并不撤回、非白名单合并转发 `violation_high + ['recall','mute','warn']`）。完整原始输出见 `docs/evidence/2026-09-18-deploy-d037-d038.md`。

**负责人解除急停与真实动作复核（14:49）**：负责人在后台按两步流程解除急停（`plan_create → plan_approve → emergency_resume`，审计 UTC 06:49:14–17）。复核结果：解除后 `action_intents` **全部 `SUCCEEDED`**（7 条撤回；`action_logs.ok=1`、`status_code=0`、无错误）；判定分布 allow 6 / record_only 20 / violation_high 7；急停期间另有 4 条 `SKIPPED`（按设计不重放，非缺陷）。

**⚠️ 部署后核查发现并修复 1 个缺陷（编号冲突，14:56 修复并重启）**：结构性规则最初误用内置规则的 `R0xx` 段——`R007` 与内置 `contextual_ad_terms` **撞号**，并被一并加入"独立硬证据"集合，后果是**内置规则**被复核门当成硬证据、其自动处罚门槛被放宽。实测影响：部署后 **3 条**判定被放宽（均"仅 R007、无其它硬证据"且 `violation_high`），**全部落在急停窗口内、动作 `SKIPPED`，真实撤回 0 次**。修复：编号改为语义前缀 `R_FORWARD_RECORD` / `R_GROUP_CARD`（脱离 `R0xx` 段）；复核门集合改为"内置三项 + decision.py 导出的结构性常量"；新增 2 条回归（编号不得匹配 `R0\d\d`、硬证据集合不得含 `R002/R004/R005/R007`）。已提权重启加载并复验：`_HARD_EVIDENCE_RULES = [R001,R003,R006,R_FORWARD_RECORD,R_GROUP_CARD]`、`R007` 不在其中、合并转发判定 `violation_high` 且通过复核门、重启后 R007-only 放宽数 **0**；全量 **1352 passed / 15 skipped / 0 failed**。详见 DECISIONS D-038 与 `docs/evidence/2026-09-18-deploy-d037-d038.md`。

**当前运行状态（2026-09-18 14:57 起）**：`QQBotWeb` / `QQBotRuntime` Running；healthz `status=ok / onebot=state=ready / connected=true / connect_count=1 / queue_backlog=0`，已在处理真实消息；**急停已解除 → 真实动作（撤回）按原配置生效**（13 个授权群、`recall_only` 阶段只撤回、不禁言）。合并转发/群名片目前**尚无真实样本命中**（重启后新规则命中数 0），群名片识别仍待真实样本验证。

**负责人反馈"带小程序码图片被撤回"的诊断（2026-09-18，只读未改动）**：见 `docs/review-2026-09-18-image-exemption-failure.md`。结论：**本次案例符合现有口径**（逐张抽查 4 张原图均为真广告；负责人截图那条"支付宝亲密号"属严重类别，按 B-2"不因校园墙外观豁免"本应撤回）；**但存在 5 个缺陷**——①**本地图片黑白名单当前完全失效**（`data/t002_media` 被 15 天清理策略删除且哈希未持久化，运行器每次启动告警 `image lists empty`，而仓库内**无样本副本可恢复**）；②`_CAMPUS_WALL_MARKERS` 校园墙文案放行信号是**死代码**（从未调用）；③微信小程序码**不可解码**，二维码白名单机制对其天然失效；④今日 237 条图片处罚中 **201 条（85%）由单主视觉模型直接决定**（无二审，与 PROJECT_CONTEXT"单模型只提供软证据"原则不一致，现由 D-022 覆盖）；⑤校园墙白名单命中只到 `record_only` 而非"放行"。另 **32 条** `category=ad` 且证据提到校园墙/小程序的记录**待人工复核**。修复需负责人决策：重新提供 8 张样本 + 是否收紧单模型自动处罚。

**D-039 已实现并落盘（2026-09-18 晚，负责人指令"带小程序二维码的都通过"），⚠️ 待重启生效**：
- 代码：`app/moderation/ai.py` 新增严格布尔字段 `has_miniprogram_code`（仅视觉通道有效、非布尔拒收）+
  `_miniprogram_qr_allow()`（命中即 `allow` + `POLICY_MINIPROGRAM_QR_ALLOW`）；`decision.py` 新增该政策标记
  （**刻意不加入 `POLICY_ALLOW_RULE_IDS`**，因为要保留例外）；`PROMPT_VERSION` 升 `t204-v14`。
- 提示词：`config/ai_prompt_rules.txt` 新增「小程序二维码·一律通过」规则并要求逐次如实输出该字段，
  同时明确区分"QQ群二维码/个人名片码/普通链接二维码**不是**小程序码"（SHA256 `414D3809…C4D68D`）。
- 配置：`.env` `AI_PROMPT_VERSION=t204-v13 → t204-v14`。
- 保留例外：①**色情 / 暴力违禁品**（**诈骗不再例外**——负责人 2026-09-18 晚修订，含小程序码的诈骗
  内容同样放行）；②本地硬证据（R001/R003/R006/`DR_`，防"配一张带码图绕过全部本地规则"）。
- 回归：`tests/test_r116_miniprogram_allow.py`（12 项）；全量 **1364 passed / 15 skipped / 0 failed**；ruff/mypy 通过。
- **部署状态（2026-09-18 16:24 重启，负责人授权 UAC）**：✅ **"诈骗不再例外"修订已真实生效**——
  Web 进程 `364`、Runtime 进程 `5196`/`25160`（16:24:31 / 16:24:37，晚于 16:03 的 `ai.py` 与
  `config/ai_prompt_rules.txt` 改动）；healthz `status=ok / mode=SAFE / onebot=ready+connected`
  （08:24:55Z 重连，`processed_total=2 / failed_total=0`）；提示词 SHA256
  `45B87843736FA4F4CC96C7D59D32576021407F2A8819EB5AA5471A5096299D9E`。
  日志无启动错误（Web：`Started server process [364]`→`Uvicorn running on 127.0.0.1:8001`；
  Runtime：`connected, shadow mode`；err 日志中的 `KeyboardInterrupt` 为旧进程被正常停止的痕迹）。
- ✅ **第二次重启已完成（2026-09-18 17:06，负责人授权 UAC）**：提示词版本号 `t204-v15` 与
  **配对来源前缀扩展**一并生效——QQBotWeb `49924` / QQBotRuntime `52132`（17:06:57 / 17:07:02，
  晚于 17:03 的 `wall_pair.py`、16:26 的 `ai.py`/`.env` 改动）；healthz
  `status=ok / mode=SAFE / onebot=ready+connected`（09:07:20Z 重连）。
- **配对规则现状（口径 C = D-036 + 两个补丁，2026-09-18 17:18 起生效）**：同成员在
  **视觉确认放行图**之后 **120 秒内**发的**文字与图片都不撤回**（降 `record_only`，转人工记录）。
  来源图前缀同时接受「校园墙白名单」**和**「小程序码通过」（D-039 联动修复；修复前只认前者，
  实测 2026-09-18 16:55–16:58 有 3 条文字因此被真实撤回）。
  **不豁免**：色情（`porn`）、暴力违禁品（`violence`）、**刷屏（`flood`，刻意保留——行为规则，
  豁免等于关掉刷屏防护）**；**结构性规则不受影响**（合并转发/群名片仍"一律撤回"，`video` 等
  未列类型不豁免）。**诈骗（`fraud`）现纳入窗口豁免**（降 `record_only` + 转人工，**非静默放行**）
  ——依据负责人两次"严重类别 = 色情/暴力"口径推定，**待负责人书面确认**；若要改回"诈骗照常撤"，
  改 `PAIR_BLOCKED_CATEGORIES` 一处即可。
- **视觉探针（负责人 2026-09-18 提供的两张原图：桌面 `0605a15a…jpg` / `2f167dc8…jpg`）**：
  用生产同款配置（`deepseek-flash` + v15 提示词）实跑，两张图均返回 `category=None`、
  `confidence=1.0`、`needs_review=False`、`has_miniprogram_code=True` → **判定放行**；
  证据前缀为「小程序码通过|校园墙白名单|文案:…」与「小程序码通过|文案:…」。
  即：**这两张图在当前口径下不会再被撤回**；历史上被撤是旧口径（诈骗仍为例外 + 配对前缀不匹配）所致。
- 附注：`/readyz` 不存在（404），健康检查以 `/healthz` 为准；本机全量测试 **1364 passed / 15 skipped / 0 failed**。
- **线上核查（2026-09-18 16:50，只读，诊断脚本用后已删）**：逐条解析最近 4000 条判定 →
  **只要视觉模型置 `has_miniprogram_code=true` 就全部放行（0 例外）**；被撤记录中命中
  `POLICY_MINIPROGRAM_QR_ALLOW` 的 **0 条**；模型自述"小程序码通过"的 8 条与字段值**完全一致**
  （不存在"说了通过但字段 false"的矛盾）。
  **被撤的图片类记录，模型给的都是 `has_miniprogram_code=false`**（证据原文如"未见校园墙特征，
  也未见小程序码"、"这是 QQ 群名片/QQ 扫码，不是微信小程序码"）。**今日 29 条 `fraud` 撤回
  全部发生在 16:24 重启前**（14:00–16:00 档 22 条），且**没有一条带小程序码标记** →
  这类图在本轮修订后**仍会被撤**。
  **结论："同图有时被撤"的根因不在规则链路，而在远程模型对小程序码的识别不稳定/漏判**
  （QQ 群码、个人名片码本就不属小程序码）。根治方向：① 用负责人手上"确定是小程序码"的原图
  做样本校准并固化回归；② **图片感知哈希白名单**（认哈希不认模型，同图二次发送直接放行）——
  该项此前因 `data/t002_media` 被清理而无样本，**需负责人重新提供原图**（见 D-039 遗留）。
- **线上运行快照（2026-09-18 16:50）**：16 个群中 **13 个启用真实动作**（3 个只审核不动手）；
  今日动作 SUCCEEDED 599 / SKIPPED 182（急停 115 + 群动作禁用 67）/ FAILED 13（均为 QQ 侧
  `recallMsg` 超时 code 1200）；累计 SUCCEEDED 1732 / SKIPPED 984 / FAILED 49。
  急停 14:34 被部署脚本置 active、14:49 由人工恢复（审计 #796 / #800），**当前非急停状态**。
- **主审 r132 三轮复验整改（2026-09-18 深夜，`7ec5553`）**：主审结论"一轮 42 + 二轮 40 全过，
  新增 23 项 4 failed / 19 passed"；四项代码/手册问题已全部修完（F02-R-2 QR 入口阈值透传、
  N01-R 同条消息内全部来源、N-F05-1 组合字段合成一次条件 UPDATE、N-F05-2 执行写锁内全量集合核对），
  F06-R 落成 `scripts/rollback_preflight.py` + `scripts/rollback_d037_d038.ps1`（`.Status` 属性、
  真实备份、可再导入名单导出与回读校验、每步退出码、失败不到达 downgrade/start）。
  **主审 23 项整改后 23/23 通过**；全量 **1509 passed / 15 skipped / 0 failed**（收集 1524）；ruff/mypy 通过。
  **仍未具备**：Windows 实机全链回滚演练证据（未停生产服务、未在生产库演练）——如实登记为待补。
  **部署：未部署**（生产仍是 17:18 加载的 `t204-v15`，本次未重启；是否部署待负责人授权）。
  违规类别累计：ad 1595 / fraud 64 / porn 20 / other 9（**撤回绝大多数是广告类**）。

## 上轮交接：2026-09-16（三项负责人政策 + R-114 复验整改全部落地）

**SHA 口径**：最新 main `c66820a`（PR #22 合并）。本日链：`a944fd8`（办证 A + 止血）→ `b087857`（卡片 D-032）→ `5162ec3`（R-114 F01/F02/F03）→ `8ae1633`（R-114 F02 残余）→ `61febee`（白名单）+ `bc3a58a`（D-033）→ `c66820a`。**Windows 实际运行 = `c66820a`**（服务已重启加载；白名单后台可用、当前 0 词）。

**负责人三项政策（2026-09-16，全部生效）**：
1. **D-031 办证/学历完全放行**——事故："办证=fraud"动态规则（反馈挖掘产物）真实误撤 2 条 → 止血（停用 DR×3：v53–v55 审计；候选 #281 拒绝）+ 全链路保护（本地豁免 allow；AI/动态规则/媒体层不得升级）+ 事故回归 `test_r113_certificate_dr_protection.py`。保留 B-2 严重词例外。
2. **D-032 群主/管理员卡片完全放行**（`test_protected_card_allow.py`；普通成员卡片对照不变）。
3. **D-033 全局白名单**——后台 `/admin/allowlist` 自助维护（增/删/启停 + AdminAudit 审计）；**仅豁免广告类**；严重类别（诈骗/色情/暴力）与刷屏**不豁免**；逐消息直读**立即生效**（迁移 `a7c3e91f0b24`；`test_r115_allowlist.py`）。

**R-114 主审复验（两轮）**：一轮（对 `2e75e1f`）F01/F03 **关闭**、F02 两处残余（同一状态契约：`degraded_reason` 洗值 + 非对象详情 `AttributeError` 中断抽样）；**已修**（`8ae1633`）：严格字符串契约（生产近 3000 条实测 2391 空 + 13 字符串 + **0 异常**，零误伤）+ 抽样入口统一解析（坏样本保留在分母标 `error` 不崩溃）；**主审原探针复验 15/15 全绿**（修复前 10 failed）；150 条重导出**仍 0 变化**；两句文档措辞同步（"无既成误删"口径 / UTC 时区）。**待主审三轮复验**。

**运行现状**：QQBotRuntime / QQBotWeb 均 Running（healthz ok、NapCat online）；动作 `ONEBOT_ACTIONS_ENABLED=true`、stage=`recall_only`、11 授权群观察期（撤回 `SUCCEEDED` 38+、NapCat 超时 `FAILED` 1 条不静默）；`data/` 4191 条目 0 链接；清理任务每天 04:00 自动运行（已为修复版）。

**待办**：①主审三轮复验（送审要点见本节）；②N03（源时间持久化/备份不豁免/样本池）；③容量 B/C 修复后重跑；④完整恢复演练 + 整机故障演练（待停机窗口）；⑤白名单首批词（负责人自行添加）；⑥对外交付前置（README 重写/License/交付方式，`NEXT_TASKS.md` D 组）。

## 上轮交接：2026-09-15 晚，**真实动作已恢复（W3+W5）+ R-111 关闭 + PR #7/#8/#9 全部合并 main**

**SHA 三口径（勿混用）**：功能提交（策略）`24d1202`；受审 head `4c57b6c`（R-111 首轮）、`bd5402c`（R-111 二轮）、`2cef2b2`（R-112）；修复链 `feb821c`→`76ce10b`→`d388ab5`。**Windows 实际运行 = `d388ab5`（2026-09-15 已部署并取生效证据）**；main 合并节点 `33f4b8d`（PR #7–#11 全部合并）。R-112 整改进行中（N01/N02/N04/N05/N06/N07 本轮修，N03 下一批）。

**主审 R-111（对 `4c57b6c`）结论**：B-2 主要行为**通过复验**（16 个端到端场景：本地严重硬命中处罚×3、独立视觉二审确认处罚×3、低置信疑似转人工保留类别×3、高置信仍要求人工×3、双视觉要求人工×3、普通广告不变×1，真实 `run_pipeline` + 迁移后隔离库、固定返回模型）；三平台 CI 全绿（[run 34874960471](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34874960471)）；**无新 P0/P1**。FAIL：1 个 P2——混合内容转人工时 ad 类别覆盖严重疑似类别（`app/moderation/ai.py` 旧逻辑 `category = local.category or usable[0].category` 先执行、severe 分支仅 None 时补救），且该类别经 pipeline 持久化 → 反馈表单隐藏字段 → 反馈存储，可能影响候选规则类别（非误罚、非静默放行）。

**主审 R-111 二轮（对 `bd5402c`）结论**：分类 P2 修复通过（严重优先 / 确定性排序 / 类别置信度同源 / 保持只记录不处罚）、P3 决策与版本口径基本完成、**无新 P0/P1**；**新增 1 个 P2**——反馈回显取「最早一条」（列表 `setdefault` 与详情 `.first()` 均无排序），与学习侧取最大写入 ID 的口径不一致：同一消息二次纠正后页面回显旧值，管理员只改原因再提交会把旧类别重新写成最新真值（主审与本机各以 3 检查点复现，真实 HTTP + 隔离库）。**已修（`76ce10b`）**：列表/详情统一按 `FeedbackRecord.id` 倒序取最新写入（与学习侧 `_latest_feedback` 的 max(ID) 同一口径；不按 created_at——同秒/时钟回拨不改变「最新」）；追加历史与审计保留（不删旧反馈、不改覆盖模型）；+8 回归（二次纠正回显×2、只改原因不回退、同秒、时钟回拨、最新 label/reason、无反馈默认值、缺 CSRF 不写库）；v13 helper 补 `source` 参数、组 B 显式 `vision`（主审 §4 非阻塞项）。

**主审 R-111 二轮复验（对 `d388ab5`）：通过**——分类 P2 与反馈再次纠正 P2 **均可关闭**，无新增阻断、无需追加产品代码补丁（逐项：最新取值 / 二次纠正 / 同秒回拨 / 历史与审计 / 无规则与处罚副作用 / 默认值鉴权CSRF / 类别校验 / B-2 / helper 精度 全通过；原 3 失败探针转 6 passed）。唯一为文档数字笔误（"新增回归 9 项"实为 8 个 pytest 用例）已在本块更正。**建议：经负责人授权后部署固定 `d388ab5` 并取生效证据；不自动合并、不恢复真实处罚。**

**首轮 P2 修复（`feb821c`，7 项新回归）**：
- `ai.py` 转人工分支：**严重疑似优先**选类别（多严重类别按置信降序 + 固定序 fraud<porn<violence 确定性选择）；**类别与置信度同源**（不得把本地广告高置信度充当严重疑似置信度）；verdict 恒 `record_only`、处罚建议恒空——**只改类别标记供人工核对，处罚判定行为不变**；办证 record_only 底线固化回归（负责人本轮强调"办证不判违规"）。
- 反馈表单类别改**可核对/纠正下拉**（默认人工上次保存值、其次系统判定值；`record_feedback_submit` 白名单校验、非法值回退 other）——落实主审建议第 4 条，避免「确认违规」被误当作「确认了系统猜测的类别」。
- 测试：`tests/test_v13_severe_policy.py` +5（两组×3类别、反序、多严重确定性、控制、办证底线）；`tests/test_admin_web.py` +2（持久化类别→表单默认选中；可纠正 + 白名单拦截）。

**门禁（绑定环境，勿混用）**：本机全量收集 **1114**、**1101 passed / 13 条件跳过 / 0 失败**（136.5s）；`ruff check`/`format` 通过；`mypy app` 85 文件通过。**主审环境**：首轮 1098 passed / 1 私有媒体跳过（收集 1099）、二轮（`bd5402c`）1105 passed / 1 跳过；**CI**：`4c57b6c` 1096+3、`bd5402c` 1103+3——各数字属不同环境，互不替代。**全部新 SHA CI 已核验全绿**（gh CLI）：`feb821c` [34877755494](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34877755494)、`4f7e90d` [34877850499](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34877850499)、`bd5402c` [34921914404](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34921914404)、`76ce10b` [34924497413](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34924497413)——三 job（Ubuntu / Windows / 干净运行时依赖）全 success。

**P3 文档收尾（本轮同步）**：本块 + `PROJECT_CONTEXT.md` + `PROGRESS.md` + 核验索引 + `DECISIONS.md` D-028；SHA 统一按三口径表述、测试数字绑定 SHA/环境；**PR #7 正文已直接更新**（gh CLI）：开头追加"最新核验入口（R-111 / v13.1）"节，旧 5739b5d/S01–S10/v7 说明保留为历史记录。

**真实部署状态（2026-09-15 已部署）**：负责人授权后已部署 `d388ab5`（工作树 `8ae6fdb`，仅文档差异）——服务重启后 healthz `ok / ready / connected`；规则 `t204-v13` + rules sha256 `8608ABC2…EE5D6`；迁移 `af66bc20f1ed (head)`。**生产后台实测通过**（真实记录两次纠正 + 只改原因不回退 + 审计 +3 + 零副作用；同证运行版本已含修复）。**PR #7/#8/#9 已全部合并 main**（`77ecaf3`/`a100fd5`/`f34c72e`）；未做生产删除、未重写 Git 历史。详见 `docs/evidence/2026-09-15-deploy-d388ab5.md`。

**真实动作恢复（2026-09-15 晚，负责人授权 W3+W5 合并进行）**：`ONEBOT_ACTIONS_ENABLED=true`（stage 保持 `recall_only`；**11 个授权群** = 首批 5 + 19:53 负责人后台扩容 6，审计 #710–730）——服务重启后**首条真实撤回执行成功**（intent #1013、群 17598122、11:39:58Z；扩容后新群亦连续成功：#1016 群 544140282、#1017 群 869142826——至记录 3 条全成功 0 失败）；重启前同场景为开关未开启 SKIPPED、未授权群继续被群动作禁用拦截（边界不变）。详见 `docs/evidence/2026-09-15-actions-resume.md` 与 DECISIONS D-030。

**R-112 主审复验（对 `2cef2b2`，2026-09-15 深夜）**：真实动作恢复本身核验通过（#1010/#1012/#1013 与代码路径一致）；另发现应用/工具增量 2×P1 + 5×P2——本机逐项独立复现（junction 误删/演练假通过/压测死变量等**全部实锤**，7 项均属实）。**本轮已修**：N01（`cleanup.py` 拒绝 symlink/junction + 删除前复核 + 4 回归）、N02（急停表述更正：后台/API 的 **DB 急停**跨进程、不重启即生效；`.env` 需重启）、N04（压测 payload 真正使用唯一文本 + 2 回归）、N05（演练按 rowid 精确校验 + 显式失败非零 + 3 回归 + 重跑 `passed=true` 再取证）、N06（"备份可读性检查通过；恢复演练未完成"）、N07（`scripts/sample_to_eval.py` 显式转换 + 4 串联回归）。**N03（源消息时间持久化/备份不豁免/样本池处理）排入下一整改批**，完成前不再声称"15 天全副本工程已关闭"。**DB 急停现场演练已完成**（负责人操作；激活 #742 → 实际拦截 #1041 授权群无外发 → 恢复 #745；见 `docs/evidence/2026-09-15-actions-resume.md` §2.2）。

**待办**（2026-09-15 晚更新）：
- **PR #7/#8/#9 已全部合并 main**（`77ecaf3`/`a100fd5`/`f34c72e`）。
- ~~真实 TUN 下载实测~~：Fake-IP 真实证据已取得，待主审核验关闭。
- ~~独立新 W2~~ → **线上抽样复测已启动**：`scripts/sample_draw.py` 首抽 150 条（`data/sample_pool/sample_20260915_1532.jsonl`，含 31 条违规正例、46 条图片）；**待人工标注真值后评测**。
- **百群负载与延迟**：已完成（基线+压测+调整+复验；`AI_DAILY_CALL_LIMIT=50000`/`AI_PER_MINUTE_LIMIT=600`/worker=10 已生效，`dd2494c`）。
- ~~隔离副本清理/恢复演练 + 旧损坏盘点~~：**已完成**（`docs/evidence/2026-09-15-ops-verifications.md`：清理逐项验证通过、真实备份可恢复、盘点清单含 `media_snapshot_20260911` 291.7MB ≈09-20~25 到期）。
- ~~通知链路~~：**QQ/邮件真实送达已验证；确认接手（ack）首次实测通过**（合集文档）；升级机制核查为设计正确（仅 page 级升级）。
- **旧副本自动清理已落地（D-029，"15 天全副本工程"）**：登记式清理（登记表 + 备份保底最新 1 份 + dry-run）；**生产 dry-run 当前 0 待删**，09-20 起 `t002_media`、≈09-25 `media_snapshot` 依次**自动**到期清理；04:00 任务自动携带（无需重启）。**R-112 N03：与 D-026"按原消息时间"的完整对齐未完（mtime 重计时/备份保底豁免/样本池处理），排入下一整改批——不再声称"全副本工程已关闭"。**原"人工核实"项关闭。
- **真实动作已恢复（负责人授权 W3+W5 合并进行）**：`ONEBOT_ACTIONS_ENABLED=true`（stage=recall_only、11 个授权群：首批 5 + 后台扩容 6）；真实撤回 #1013/#1016/#1017 全成功；**转入观察期**（撤回成功率/NapCat 超时/审计），异常即回退（改回 false+重启或拉急停）。详见 `docs/evidence/2026-09-15-actions-resume.md`、DECISIONS D-030。
- **剩余（明确）**：故障演练（断网/关机，待停机窗口）；线上抽样人工标注真值后评测；真实动作观察期结论；外部心跳（**负责人已决定不配置**，风险自留）。

## 上轮交接：2026-09-15，R-110 / v13 严重类别策略整改（功能提交 `24d1202`、受审 head `4c57b6c`；历史）

**最新 SHA `24d1202ea2efe69d81a14c2246392bdc69476a07`**（分支 windows-deploy-2026-09-10），已推送。提交链：`edf618a` → `7dcee6a`（应用 R-109 累计补丁）→ `6e363dd`（v12 提示词）→ `4e26252`（索引）→ `24d1202`（v13 策略整改）。

**禁止重复应用 R-109/R-108 累计补丁**——补丁已完整落地于 7dcee6a；重复叠加会冲突或回退修复。

负责人决策（2026-09-15，方案 **B-2** + 场景③小修）：严重类别（诈骗/色情/暴力违禁品）不因校园墙特征或办证词豁免；**本地规则高置信命中直接判违规+处罚建议**；**高置信确认 + 独立二审消疑照常自动处罚**；**低置信疑似严重类别保留类别转人工记录，不得静默放行**（本次代码修复点，ai.py merge 尾部）；纯办证转人工记录不处罚。提示词 **t204-v13** 与代码行为一致。11 项正式回归（tests/test_v13_severe_policy.py）覆盖三场景+控制组+4 项边界（混合命中按严重类别处罚、AI 错误判正常不覆盖本地处罚、单模型高置信照常处罚、受保护成员不处罚）。

本地门禁：全量 **1095 passed / 13 条件跳过 / 0 失败**；ruff check / format 205 文件 / mypy app 85 文件全部通过。新 SHA 的 GitHub CI 结果以 [PR #7](https://github.com/miaomiao636/qq-group-moderation-bot/pull/7) 检查页为准（本机无凭据查询私有仓库 Actions）。上一受审 SHA `4e26252` 的 CI 三个 job 全绿（run 34867961578）。

真实部署状态：Windows 服务已重启加载本版本（healthz ok/ready，动作 `ONEBOT_ACTIONS_ENABLED=false` 撤回保持关闭）。**恢复真实动作、生产删除、Git 历史重写均未授权、未执行。**

主审 R-110 验收报告的 PASS：R-108/R-109 补丁复验通过；FAIL（本块已处理）：v12 提示词无代码约束（场景①本地短路照罚、②二审消疑照罚为负责人批准的 B-2 期望；③低置信静默放行已修复为转人工）。待办（沿用 R-109 报告 4/5 节）：真实 TUN 下载实测、隔离副本清理/恢复验收、独立新 W2、百群负载与 17.4s 延迟、通知接管/整机故障演练、授权后的 W3-W5。

## 上轮交接：2026-09-15，PR #7 R-109 累计补丁（edf618a，历史）

统一入口：`docs/pr7-r109-review.md`。补丁唯一基线 `edf618a3287a820879d3e3b681bb62748ca4d78b`；远端收尾核对仍为此 SHA。**本轮累计补丁已包含 R-108，禁止再叠加旧补丁。** 受审 SHA 的 CI run `34861669318` 双平台 pytest 实际失败（各 1 failed / 903 passed / 3 skipped），不是全绿；补丁尚未提交/推送或产生新远程 CI。

新增归档/清理保护：保留违规最小行与编号、核对双向引用及身份/时间链、仅 CLOSED/安全关联才清非必要内容、最小审计与幂等标记、风险不明计 cases_cleanup_deferred。规则启停改为显式意图，ACTIVE 复制草稿，仍需预览确认发布；候选内容清理保留 PURGED 编号，已复制关联跳过。归档筛选/恢复/坏附件及 .mkv/.pdf 回看已修。旧 R-108 安全下载、配对时序/重试、AI 字段、办证分类及通知消费者回归均保留。

最终全量 **1087 passed / 1 私有媒体 skipped / 1 既有弃用警告，114.99 秒**；Ruff、format 204 文件、原生/Windows 目标 mypy 各85文件通过。新增迁移数据保持测试及隔离浏览器点验通过；实际截图验证归档恢复及规则发布确认入口。没有真实外呼，QA服务已关闭。补丁没有另增迁移，但 edf 本身有 `af66bc20f1ed`，部署必须核查；downgrade 会丢归档标记。

Windows 接手优先：确认真实动作仍关闭、暂停旧自动/手动清理→本机改动/基线盘点→隔离应用累计补丁→门禁与新 SHA CI→隔离副本清理/恢复/规则发布回归→获准部署与恢复清理。若 edf 已清坏关联或丢审计，本补丁不会自动恢复历史数据。全部原文副本按原消息15天的工程、百群负载/17.4s延迟、新独立W2、通知ack/整机故障、授权W3/W4/W5仍待办；详情、估时及关闭证据见统一报告。

本轮未推送/合并/部署/重启、未恢复处罚、未清理生产数据或重写历史。保留原开发工作树。需负责人确定：例外与严重类别优先级、历史原文清理授权、实测后性能/费用/人工响应目标；已经确认的15天原文政策不重复索问。D-027记录新增数据管理和版本保护的技术边界。

## 上轮交接：2026-09-14，PR #7 R-108 复验与本地补丁（历史）

受审基线 `7d1f76152ac566bf5356557d567b461186f69c39`，远程同 SHA CI run `34835085195` 实际失败（Ubuntu/Windows pytest 各 1 failed / 899 passed / 3 skipped），不是实施方自报的全绿。Windows 已暂停动作是实施方回报，主审未访问生产验证。先读 `docs/pr7-r108-review.md` 与 D-026，不沿用下方历史发布授权合并、部署或恢复处罚。

本轮在隔离工作树修复：单次 DNS 分类并固定目的 IP/明确 CDN；生产 AI 字段严格确认；完整发送顺序及 pending Inbox/活跃 worker 上下文；源图重试原子保留绑定、完整事件键及账号隔离；办证广告策略贯穿 AI、混合严重关键词正确分类；15 天原文清理保留规范化时间元数据及旧清理记录兼容。危险删除路由保持停用，补实际 HTTP/CSRF/纠错回归，不借机恢复删除或添加归档。

没有把处理中状态提前写为 Shadow 最终判定，以保护既有通知游标；补晚完成、正常消息与乱序通知回归。活跃上下文仅最小无原文元数据、token finally 清理，恢复仍靠 durable Inbox。没有新增依赖、迁移、生产配置、通知外发、模型切换或处罚阈值调整。

精确全量验证：1013 passed / 1 私有媒体 skipped / 1 既有弃用警告，116.38 秒；Ruff、format 198 文件、原生/Windows 目标 mypy 各 85 文件及 diff 检查通过，详见 `docs/pr7-r108-review.md`。本轮未提交/推送远端、未合并、未部署/重启 Windows、未恢复动作、未删除生产数据或重写 Git；交付为固定目录的报告与可应用补丁。原开发工作树未修改。

Windows 依序：核对基线/本机改动→隔离应用补丁和门禁→获准后更新且动作关闭→真实 TUN 合成图片与并发/重试场景→独立新 W2/百群峰值与延迟→通知接管/整机故障→获明确授权的隔离真实动作和小范围放量。统一 15 天所有副本政策已确认，但源时间台账、备份/快照/导出到期、无人登录清理及容量告警仍需实施；不要把文档写完当作清理完成。

需负责人决定：绝对放行与严重类别的统一优先级；受控 Git 历史重写授权。原内容保留 15 天及接受 16–30 天首违原件缺失已明确，不再重复索问。归档与查询索引排 P2；容量指标、费用与人工响应目标必须用 Windows 实际数据，不凭旧 1–10 群假设认定百群可交付。

## 上轮交接：2026-09-12，PR #7 R-107 主审定向修复

受审基线 `ddf1f73214015d8df6eff71606b6f3ade4909ea3`，PR #7 / `windows-deploy-2026-09-10`。负责人授权：修复后本地验证、提交并推送现有 PR；**暂不合并、不部署 Windows、不改生产配置或开启真实处罚**。最终推送 SHA 与该 head 的 Ubuntu/Windows/干净运行时 CI 以 [PR #7](https://github.com/miaomiao636/qq-group-moderation-bot/pull/7) 的最新提交/检查为准；历史 ddf1f73 的 CI 不能替代新结果。

先读 `docs/pr7-r107-review.md` 与 D-024，再读本节对应的 Windows 待办。不要继续按下方历史 R-106/PR #5 的“允许合并”授权操作本轮。原开发工作树保持原状，本轮在独立工作树实施。

交付修改：视频独立临时抽帧、非零退出拒绝部分帧；W2 合并/导出保留不可用标记，显式冻结版本声明、只取 DONE 延迟、防覆盖；E2E 只读精确内部键关联、闭合 UTC 窗口和完整状态/缺失数量；媒体清理失败可见；受管影子/反馈/违规引用/缓存/未确认自动候选原文清理，保留累犯与误判撤销身份、人工标签和规则配置。无迁移或依赖变化，没有更换模型、放宽门槛、增加并发或更改真实通知。

最终实际本地验证：**860 passed / 1 skipped / 1 原有 datetime adapter 弃用警告，108.41 秒**；跳过仅私有真实媒体样本，SQLite 路径守卫全部执行。Ruff check/format 188 文件、原生/Windows 目标 mypy 83 源文件、diff 检查及文档 JSON schema 通过。先红后绿的定向测试与独立复核见报告。开发机没有 Windows 实机、生产数据库或原始 QQ/邮件私有证据；本轮测试没有真实外呼，亦未执行生产清理。静态页面仅纠正保留期文案，未做浏览器视觉 QA。

Windows 后续按顺序处理（实施部署/生产变更仍需负责人授权）：

1. 核对 PR 最终 head/CI 与本机未提交修改、实际数据库路径和配置，保持 SHADOW / 真实动作关闭；不要强拉覆盖本机修订。原内容保留 15 天是负责人明确取舍，接受第 16–30 天首违原件缺失，不再询问是否豁免到 30 天。
2. 新版清理会比旧版多处理原文副本。先在受控隔离副本检查新增计数、30 天累犯/误判纠错/规则保留、文件占用时 failed；生产定时任务实际运行与变更前范围核对仍需留证，文件删除不能靠回滚数据库恢复。
3. 单独盘点 `media_snapshot_20260911`、数据库备份、W2 手工导出/标注、历史 `_frames/frame_*.png` 与崩溃留下的临时目录；它们不是全部由媒体顶层清理器自动过期。依据 15 天规则核对归属和年龄，未经确认不要递归删除未知目录。04:00 登录用户任务的漏跑/无人登录与失败监測仍待 W4。
4. 用新 `w2_e2e_latency_report.py --database ... --since ... --until ... --out <新文件>` 复算；保留旧证据不覆盖，原件已清则明确无法复算。旧文字 p95=17.4s/3s 门槛尚未解决；当前已有异步队列、纯文字不调用视觉二审。冻结版本采集实际 AI/排队/其他处理证据后再讨论收紧超时、模型选择或 SLA 拆分，不先改阈值。
5. 补未参与优化的新独立样本，15 天内核对完整原件/上下文，人工真值→merge→正式报告；不能用截断预览或原 160 条开发集替代。部署后持续补新漏报与困难负例，候选到规则仍由人工确认发布。
6. QQ 真人收件属于已报告确认；应用内 ack、QQ 失败→邮件回退、备份接管与整机失联/恢复仍需实测。独立 W2/延迟与安全门禁未过，不放行 W3 真实动作或无人值守交付。

## 上轮交接：2026-09-10，R-106主动通知（以下为当时状态）

用户要求落实QQ管理群业务提醒、独立邮件故障告警及外部健康心跳方案，并已明确授权全面复验通过后推送GitHub、合并，余项交给Windows。本地分支 `feature/r106-proactive-notifications` 基于已合并main `15278e84560cdc11adab40e3d3225fa63634cc2a`，工作树 `/tmp/qqbot-pr5-round2.OZ4N0s`；原项目工作树保持原状。发布核验规则：在 [R-106 PR #6](https://github.com/miaomiao636/qq-group-moderation-bot/pull/6) 查看最终head的三项CI、合并状态及验收评论中的精确SHA；未合并或最终head检查未通过不得部署。PR #5不包含R-106，Windows不能仅拉PR #5就认为通知可用。

交付代码：`app/notifications/`包含配置、三表、持久状态机、采集/恢复、发送、后台调度及独立watchdog；`app/web/notifications.py`真人接手（不处罚）；main挂载与生命周期、OneBot worker健康seam、cleanup+maintenance计数对接；迁移 `d3f5a7b9c111`（仅增表，父 `c2e4f6a8b010`）。配置模板全关闭，没有真实收件人/令牌。D-023与 `docs/proactive-notifications.md`记录设计、实际命令、阈值、故障处理、回退和Windows验收。

最终实际验证：pytest **756通过/1跳过/1原有datetime弃用警告，111.37秒**；原生/Windows目标mypy83源文件、ruff check/format172文件、diff及离线锁文件检查通过。受限环境曾阻止两项启动测试绑定回环端口，允许本地测试端口后整套重跑通过，没有删测试或放宽断言。临时库迁移升降级、旧数据保留、schema一致性通过；干净锁定仅运行时安装及导入通过且无pytest，默认探针运行返回skipped。两轴复核剩余0：保留期后UNKNOWN/QQ回退不重弹、排队ACK终检、定时维护新增计数、并发停机、AI真实成功恢复、邮件故障可见均补红后绿回归；最终补通道预留名额，新增9项公平性回归及独立阻塞/确认/取消/UNKNOWN探针均通过。发送测试完全fake，未真实发QQ/邮件/心跳，未部署Windows。

明确缺口：Chrome控制和内置浏览器均不可用，视觉/窄屏/键盘QA未执行；已关闭独立临时零外呼QA服务，未动真实库。Windows须补页面验证、真实送达/接手/升级、服务退出/QQ退出/DB不可写/断网关机与恢复、任务计划无人登录运行；外部Healthchecks需另配首位/备份邮箱与缺失检测。应用内15分钟升级不能在整机断网/关机时运行，不是外部SLA保证。

发布注意：PR #6首轮CI `34458814457` 的Windows因SMTP测试过早断言已发送而失败，尽管UNKNOWN和smtp_busy保护均已生效；受控TLS慢启动3/3复现后，改为发送前/发送中事件同步，仍使用真实20ms超时与shield，并有无包装慢启动用例；取消测试实际等待线程退出。生产发送器未改，39项发送测试连续10轮、关联113项及独立复核通过。不得拿首轮失败或旧head的两项绿替代最终三项通过，应核对PR验收评论中的修正提交与CI。

下一位Agent：先读本节及两份Windows清单，确认获准发布的R-106 SHA与其CI后再让Windows更新；本机备份→迁移→保持SHADOW与处罚关闭→获准配置并逐个启用通知→隔离实测与脱敏证据。不要复用管理/Agent/OneBot令牌做健康token，不删除去重水位“重置”告警，不在群中自由文本回复就替代真人接手/处罚审批；已跨过网络发送边界的请求不能保证取消，UNKNOWN不要重放。T-303原件/独立W2等原现场缺口不因通知代码完成而关闭。

## 上轮交接：PR #5 R-105主审整改（以下为当时状态）

本次主审从PR首轮 `6304d35` 在隔离工作树修复，未覆盖原工作树。最终本地集成门禁与两轴复核通过；远程CI及合并SHA以PR新评论为准。本轮验证详见本节与 `docs/pr5-r105-acceptance.md`，不采信下面历史数字为本轮结论。

已处理原10项复验中的缺陷，并实现剩余代码项#9 provider设置迁移、#10账号绑定、#12持久接收；#16 PR边界与#17文档随本轮交付。条件双模型保留，疑难复核失败不自动处罚；完整真人审批、DB急停、唯一动作出口、下载防SSRF与超时、幂等领取、人工标签撤销有效违规、WAL一致备份、维护CLI及独立评测CLI均有回归测试。完整逐项表见验收报告。

仍未关闭：P1-13 Windows T-303原件/脱敏证据、P1-14真实独立W2效果。历史937条、服务恢复等是实施方自述，主审没有访问那台Windows电脑，不得补造证据。动作仍默认SHADOW、OneBot关闭、阶段recall_only；合并/重启不代表可自动全开。没有执行QQ动作、没有自动踢人。

Windows Agent从 `docs/windows-delivery-checklist.md` 开始：查实际部署与本机修改→急停/关闭动作→一致备份→固定合并SHA→锁定依赖/迁移→W0/W1→真实SHADOW效果与临时DB假动作回放W2→负责人明确授权的隔离W3→恢复W4→目标群灰度W5。真实SHADOW本来就不累计处罚或生成处罚案件，不要为此偷开动作。只能在实际服务停止后恢复备份，并核对旧记录与真实QQ状态，未知结果不得重发。

本轮迁移head `c2e4f6a8b010`：保留历史表及旧字段、清除旧动作授权；升级后逐群重新由真人批准。SQLite是首版受验数据方案；单Web/OneBot实例，禁止多worker，非当前使用的官方运行器不应同时启动。维护CLI已实现但Windows任务计划尚未注册/实测；后台通知仍不是主动推送，客户需明确接管人、渠道与响应目标，未完成不能按无人值守服务签收。

本轮验证记录：完整pytest **556通过/1跳过/1个旧datetime弃用警告（103.60s）**；mypy71源文件、ruff check、format144文件、diff检查通过；临时库完整升降级与metadata一致性、旧授权安全迁移、干净仅运行时安装/导入且pytest缺失均通过。最终同成员动作链顺序亦经独立probe复验；GitHub Ubuntu/Windows/干净运行时三项以本轮最终head的PR评论与检查结果为准。跳过本地私有媒体样本，不能宣称真实模型或真机覆盖。

## 历史实施报告（保留来源，以下“当前”仅指当时）

R-105远程平台补验：首轮本轮CI `34447804238` Windows因fcntl静态分支判定失败而未合并；已改用sys.platform并移除Any绕行。本机与Windows目标mypy均通过，锁/入站/纠错55项通过；以PR最终head的新CI核对完整跨平台结果。该记录属于当前交接补充，不复用失败运行作成功证据。

## 日期

2026-09-09晚（T-303收尾 + T-307实现 + Windows服务化）

## 当前任务

**T-303 24h影子数据汇总完成 + T-307 OneBot动作Adapter实现 + NSSM服务化**。
- T-303窗口满24h，937条判定/936次AI调用/action_intents=0，证据 `data/t303-report.md`；断线演练三项通过 `data/drill-log-2026-09-09.md`。**待主审验收。**
- T-307在分支 `feature/t307-onebot-actions` 实现：撤回=delete_msg/禁言=set_group_ban/警告=send_group_msg，经反向WS echo出站；数字ID强制校验；发送前未就绪=FAILED，发送后超时/断线=UNKNOWN冻结不重放；`ONEBOT_ACTIONS_ENABLED` 默认关闭，独立于ACTION_MODE第二道开关。29项新测试，全套320项通过、mypy 65文件、ruff通过。**未自行宣布验收，待主审独立审核。**
- NSSM服务化：`QQBotWeb`/`QQBotRuntime` 已注册（开机自启/崩溃5s重启/日志轮转），NapCat启动脚本入启动文件夹；崩溃重启实测通过。**真实动作隔离群实测属W3，需T-307验收后进行。**
- 切OFFICIAL前置：T-307验收 + ONEBOT_ACTIONS_ENABLED=true + 按群route(onebot→onebot) + 按群action_enabled + 急停关。当前默认仍SHADOW。面板勾"动作"已自动补齐同通道路由（方案A），完整流程见 `docs/switch-official.md`。

## 已完成内容

### 架构审查8项修复 + T-303实机UX改进（2026-09-09）

**修改文件清单**（10文件，+437/-33行）：

| 文件 | 改动 |
|---|---|
| `app/__main__.py` | ⑧ uvicorn `ws_max_size=1_048_576`（1MB WS帧上限） |
| `app/main.py` | ⑤ lifespan启动时调用`reap_stuck_leases`清理过期租约+统计PENDING |
| `app/moderation/ai.py` | ① `merge_ai_evidence`加AI置信度门槛：非正常类+≥0.60才升级record_only |
| `app/adapters/qq_official/media.py` | ② `_is_safe_media_url`+`_BLOCKED_NETWORKS`SSRF防护 |
| `app/core/dedup.py` | ⑤ `reap_stuck_leases`函数 + ④ `mark_pending`函数 + PENDING加入`_claim_existing`可领取条件 |
| `app/runtime/onebot_ws.py` | ⑥ worker并发3（`asyncio.gather`独立引擎）+ ⑦ `_ensure_worker`重建前`cancel()`旧task |
| `app/runtime/onebot_wiring.py` | ④ `process_onebot_event`处理前调`mark_pending` |
| `app/runtime/pipeline.py` | detail补存`media_files`（供详情页回看原图） |
| `app/moderation/feedback.py` | ④ `record_recall_notice`撤回通知自动记录 + `record_feedback`重复提交改为更新 |
| `app/web/routes.py` | 详情页+媒体端点+反馈回显+datalist预设+30秒智能刷新 |

**验证结果**：
- `uv run ruff check app`：All checks passed
- `uv run ruff format --check app`：61 files formatted
- `uv run mypy app`：61源文件0错误（strict）
- `uv run pytest`：**291 passed**（1个e2e flaky单独通过）
- 服务重启：NapCat 5秒自动重连→`ready`
- healthz：`self_id`/`group_last_event`不再暴露（`include_sensitive=False`）
- 僵尸租约：旧1条过期PROCESSING→FAILED(lease_expired)已清理
- 负责人已预同意：T-002样本达标+AI评测一致率满足后，升级AI参与自动处罚的政策（需走DECISIONS流程）

### 历史交接

2026-09-08（T-306主审整改轮）

## 当前任务

**T-306 NapCat/OneBot入站Adapter与影子运行器已完成独立主审整改并验收。** 修复提交 `444b368` 的远程CI运行 `34219858155` 在Ubuntu、Windows和干净运行时三项全绿。下一项进入 T-303（Windows隔离群W1影子验证），随后 `T-307 → T-404 → T-403`；T-304人工批准踢人为T-307之后的可选增强。

## 已完成内容

### T-306独立主审整改（2026-09-08）

- 未直接采信首轮交接和旧CI：复验发现媒体路径逃逸可读取受管目录外文件、`0.0.0.0`/`::`被错误允许、详细状态无鉴权且健康端点泄露标识、离线心跳误报ready、缺失`self_id`进入unknown命名空间、跨账号同消息ID覆盖、重复事件在去重前重复下载、URL查询令牌泄露及OneBot解析器中转依赖官方Adapter。
- 以上问题均已添加回归测试并修复；详细状态与WS只接受Bearer，公开健康状态脱敏；下载失败不回退原始路径，解析文件限制在受管目录；去重先于媒体下载；影子内部键使用`onebot:self_id:message_id`并由新列`external_message_id`保留外部ID。
- 新增Alembic迁移 `f6a2c7e91b40`；独立临时库验证旧值回填、降级到`d4f7a9c2e601`和再次升级成功。
- 本地证据：291项收集，290 passed / 1 skipped；mypy 61源文件；ruff 106文件；`uv lock --check`；独立`--no-dev`运行时导入且pytest缺失，均通过。
- 远程证据：PR #3修复提交 `444b368`，CI运行 `34219858155`；干净运行时job `102040156438`、Ubuntu job `102040156595`、Windows job `102040156652` 均为success。T-306已关闭，下一项为T-303/W1。

### 历史：T-306首轮实现（2026-09-08，实现Agent）


**任务边界遵守情况**：
- 未实现T-307：OneBot Adapter包内不存在任何管理动作实现（撤回/禁言/警告/踢人），AST测试守护；未实现任何真实动作路径或按群真实开关。
- 未执行任何真实QQ群动作；影子模式边界未变，OneBot事件动作意图数为0有断言。
- 未实现T-303/W1实机内容（无真实QQ/NapCat配置、无实机取证）。
- 未绕过/讨论QQ安全验证、设备指纹或风控。
- 真实令牌只存在于Windows本机环境变量；仓库内只有 `.env.example` 变量名与测试假令牌（`test-onebot-token`）。

**实现清单**：
1. `app/adapters/onebot/parser.py`：OneBot 11群消息→T-305中立契约纯转换。文字/at/face/image(.gif判GIF)/mface/record/video/file/reply/forward/json卡片/share链接分享全覆盖；未知段保留"类型名+数据键名"元数据转`kind="unknown"`并加文本标记；CQ码字符串形态降级；匿名标记；角色映射（owner/admin/member）；`_downloaded`按附件序号回填本地安全文件名。数字ID一律字符串进入`external_*`字段，绝不伪装OpenID。
2. `app/runtime/onebot_ws.py`：反向WebSocket端点（`ONEBOT_WS_PATH`，默认`/onebot/ws`）+`OneBotRuntimeStatus`状态注册表。令牌常量时间比较（query或Bearer）；群消息入口校验`message_id/group_id/user_id`三要素；JSON非法帧计数容忍（连续50帧断开1002）；lifecycle connect→登录态online；heartbeat→心跳时间。`ready/degraded`：已连接+登录online+心跳新鲜+积压未满，任一不满足即degraded；`/onebot/status`与`/healthz`的`onebot`块暴露连接数/登录态/最后心跳/每群最后事件（有界200）/队列积压/处理计数。有界队列（`ONEBOT_QUEUE_MAX`）满时阻塞接收形成TCP背压不丢事件；worker惰性启动并按事件循环重建。
3. `app/runtime/onebot_wiring.py`：组合根。`dedup_key_for`=`onebot:{self_id}:{message_id}`；复用R-102-4 `download_attachment`（大小/类型嗅探/磁盘配额/超时/安全文件名），仅接受http(s) URL即时下载，本地路径视为不可信→失败→record_only；绝不执行群成员文件。
4. `app/runtime/pipeline.py`：`dedup_key`参数（持久化去重键与provider记录）；通道中立守卫`_contains_unreviewable_content`——`kind`或任一段为`unknown/forward_record`强制`record_only`转人工；detail附中立段摘要。官方路径行为不变。
5. `app/core/dedup.py`：`begin_processing`新增`provider`参数落库。
6. `app/config.py`：新增`ONEBOT_WS_ENABLED/ONEBOT_ACCESS_TOKEN/ONEBOT_WS_PATH/ONEBOT_QUEUE_MAX/ONEBOT_HEARTBEAT_TIMEOUT_SECONDS`；启用时fail-closed校验（令牌非空、WEB_HOST回环/私网、路径以/开头），违者拒绝启动。默认关闭，官方链路与既有测试零改动。
7. `app/main.py`：启用时挂载OneBot路由；`/healthz`附带`onebot`就绪块。
8. 测试：9份新脱敏fixture + 23项新测试（`test_onebot_parser.py` 20项、`test_onebot_ws.py` 17项：鉴权拒绝/ready-degraded切换/全流程影子处理/去重键落库/重复推送+重连+缓存清空模拟重启/非法JSON存活/非法结构不落库/媒体下载失败降级/文件无URL与未知段与合并转发转人工/动作意图0断言/AST守护无管理动作/配置fail-closed 4项）。

**远程CI证据（PR #3，head提交 `1dd6725`，运行 `34215504894`，结论 success）**：
- 分支：`feature/t306-onebot-shadow`（已推送并跟踪origin）。
- Pull Request：https://github.com/miaomiao636/qq-group-moderation-bot/pull/3（目标 `main`，**保持open等待主审审核**，未合并）。
- 运行总览：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34215504894
- Ubuntu质量（ruff/mypy/pytest）：`.../job/102026185851` `SUCCESS`
- Windows质量（ruff/mypy/pytest）：`.../job/102026185971` `SUCCESS`
- 干净运行时依赖回归：`.../job/102026185841` `SUCCESS`
- 执行备注：推送与GitHub API经本机既有代理 `127.0.0.1:7890`（git `-c` 单次参数与urllib代理，未改任何git配置）。

**本地验证结果**：
- `uv lock --check`：通过（52包，无变更）。
- `uv run alembic upgrade head`：开发库由 `a0b4d72e5f31` 迁移至head `d4f7a9c2e601`（T-305迁移链），无新迁移（T-306无schema变更，复用T-305的provider列）。
- `uv run ruff check app tests alembic`：All checks passed。
- `uv run ruff format --check app tests alembic`：104 files already formatted。
- `uv run mypy app`：61源文件通过（strict）。
- `uv run pytest`：**276 passed**（基线234 + 新增42项收集）。
- 干净运行时依赖：独立venv `uv sync --locked --no-dev` 后 `app.main`/onebot解析器/组合根/WS模块全部可导入，pytest不可导入。
- Git修改范围：6个既有文件小改（config/dedup/main/pipeline/conftest/.env.example）+ 新增 `app/adapters/onebot/`、`app/runtime/onebot_ws.py`、`app/runtime/onebot_wiring.py`、9份fixture、2个测试文件。
- 敏感信息扫描：diff与全部新增文件无真实AppID/AppSecret/API Key/个人QQ号/真实令牌；fixture全部虚构数字ID。

**已知风险与未完成事项**：
- **未验收**：等待主审按T-306完成标准独立复核，不自行宣布验收通过。
- 事件队列为进程内存队列：进程崩溃窗口内未处理事件丢失（消息缺口），须在T-303/W1用真实NapCat实测量化；去重只保证"不重复"，不保证"不丢"。
- OneBot文件消息常无内联URL（需API二次获取）：当前一律下载失败→record_only转人工；文件URL获取属T-303/T-307间的增强项。
- 回复/引用仅保留引用目标ID元数据，被引用内容需API查询（当前不获取）。
- 合并转发内容不在事件内：只记骨架转人工；转发内容展开审计留待后续增强。
- QQ登录态依据lifecycle connect与心跳新鲜度推断，未调用`get_status`类API；QQ被强制下线但NapCat仍连着WS的场景检测依赖心跳语义，T-303实测校准。
- WS传输未加密（ws://）：安全前提是仅本机/可信内网绑定（配置强制）；跨网段部署需反代TLS，留待部署期决策。
- 远程CI（Ubuntu/Windows/干净运行时）结果见下方CI小节；CI通过不等于T-306验收。
- 真实NapCat、真实MiMo、Windows 24×7仍全部未验收（与基线一致）。

### 历史交接

### T-305主审整改轮（2026-09-08）

## T-305主审整改摘要

- 修复P0：路由改为`message_provider + external_group_id`联合键；未配置OneBot、交叉provider或非法路由默认拒绝，不再回退官方API。
- 修复P1：违规累计、案件复用和动作幂等纳入provider+中立身份，不会因两通道ID文本相同而串案。
- 修复P1：通用去重上移`app/core/dedup.py`，审核核心、案件、编排与流水线无供应商Adapter反向导入，并新增AST回归测试。
- 修复P2：新增通用`MessageSegment`，官方和OneBot fixture都转换为中立段，不透出原始CQ/官方结构。
- 动作边界：`ACTION_MODE=OFFICIAL`只能调官方Adapter；OneBot只记SKIPPED，必须等T-307的独立配置。
- 数据库：新增纠正迁移`d4f7a9c2e601`，完整`upgrade head → downgrade base → upgrade head`通过。
- 验收证据：本地234项收集，233 passed / 1 skipped；mypy 57源文件、ruff check/format、`git diff --check`通过。远程CI运行 `34200777456` 三项全绿：Ubuntu job `101978815953`、Windows job `101978815792`、干净运行时 job `101978815991`。
- CI维护：提交 `3377279` 将`actions/checkout`升级至v7.0.1、`astral-sh/setup-uv`升级至v10.0.1并固定提交哈希；运行 `34203205684` 三项全绿，Node.js 20弃用警告已消除。

## Windows专机待执行事项

- **现在不需要真机动作测试**：T-305是契约与迁移任务，GitHub Windows CI通过即可关闭跨平台代码兼容门禁。若在Windows本机拉取该分支，先执行`uv run alembic upgrade head`，当前head应为`d4f7a9c2e601`。
- **T-306后执行W1/W2**：安装并固定QQ/NapCat版本，在隔离群验证全消息类型、媒体下载、重放去重、断线重连、QQ登录态和连续24小时影子运行；外部管理动作调用数必须为0。
- **T-307后执行W3**：只在隔离群分别测试撤回、禁言3600/86400秒、首次警告、保护角色、急停、重复事件和`UNKNOWN`人工复核。
- **T-404后执行W4**：验证锁屏/熄屏、禁止睡眠、Windows Service自启、强制终止恢复、断网、系统更新重启、备份恢复和外部心跳。
- **W1至W4均通过后才做W5**：目标大群先影子、再逐群开撤回/禁言/警告；踢人首版可始终由人工QQ客户端执行。详细进入/退出标准见`docs/windows-operations.md`。

## 已完成内容

### T-305 实现（2026-09-08，实现Agent）

**任务边界遵守情况**：
- 未实现T-306（无OneBot WebSocket入站、无NapCat运行器、无真实媒体下载）；`tests/test_onebot_fixture.py` 中的 `OneBotFixtureSource` 是**测试内最小映射**，仅为证明核心链路可被OneBot fixture驱动。
- 未实现真实撤回/禁言/踢人：影子模式边界未变；`ModerationActionClient` 协议只有 recall/mute/warn，无kick；新动作路径默认SHADOW下调用数为0并有测试断言。
- 未修改任何真实凭据：diff与新增文件扫描无真实AppID/AppSecret/API Key/个人信息；fixture全部使用虚构数字ID。
- 未执行任何真实QQ群动作。

**实现清单**：
1. 新增 `app/core/contracts.py`：`Provider`（qq_official|onebot）、中立 `StandardMessage`（provider/external_group_id/external_user_id/external_message_id，与旧字段构造时双向同步的镜像视图）、`Sender/Attachment/ShareCardInfo/ActionResult` 上移、`MessageSource`/`ModerationActionClient` 位置限定参数协议（runtime-checkable）。
2. 新增 `app/core/routing.py`：`GroupProviderRoute` 以 `message_provider + external_group_id` 为联合键；`resolve_action_provider` 对未配置OneBot、非法值和交叉provider一律fail-closed，仅旧官方链路保留兼容默认；`upsert_group_route` 拒绝未经映射的跨provider配置。
3. 新增 `app/core/identity_backfill.py`：回填SQL单一事实来源（幂等只补空，迁移与测试共用）。
4. 新增 `app/actions/official_wiring.py`：官方动作客户端组合根（凭据缺失→未配置→SKIPPED意图，不异常）。
5. 契约上移与兼容再导出：`app/adapters/qq_official/contract.py`、`actions.py` 保留原导入路径；`QQOfficialMessageSource` 实现seam。核心模块（moderation/cases/actions/reports/core）顶层零 `from app.adapters` 导入（静态扫描验证）。
6. 动作编排 `orchestrator.py`：按消息provider与群ID联合解析动作路由；`ACTION_MODE=OFFICIAL`只能构建并调用官方客户端，OneBot路径在T-307前始终生成SKIPPED意图且不能注入客户端绕过；`ActionIntent`/`ActionLog` 双写中立身份；审计改为编排器内中立写入（原官方 `audit.log_action` 保留未删）。
7. 案件服务：违规/案件双写中立身份；窗口计数与案件幂等查询以中立列为准，兼容仅旧镜像列的混合行（`or_` 回退条件）。
8. 流水线：新增 `message_source` seam参数（缺省官方解析器，行为不变）；解析失败兜底身份兼容官方/OneBot键名；影子判定双写。
9. 反馈/AI用量：`FeedbackRecord`、`AIUsageLog`（external_group_id；其provider列语义为AI供应商故不复用）、`AIModerationRequest` 双写/透传中立群标识。
10. Alembic迁移 `b8e2f6a4c1d9`（down_revision `a0b4d72e5f31`）：8张表batch加列（provider/external_*，含server_default）+回填+`group_provider_routes`表；downgrade完整逆操作。已验证 `upgrade head → downgrade base → re-upgrade head` 周期。
11. ORM同步加列：`app/runtime/models.py`、`app/cases/models.py`、`app/models.py`、`app/moderation/feedback.py`、`app/moderation/ai.py`；`ModerationDecision` 增加中立字段（双向同步validator）。
12. **测试封闭性修复**：`tests/conftest.py` 强制 `AI_ENABLED=false`。缺陷背景：开发机 `.env` 配置 `AI_ENABLED=true` 时，pytest审核链路会真实外呼MiMo（T-204 factory按settings构建），限流/超时导致 `allow/record_only` 判定漂移、测试非确定失败。项目规则本要求AI测试只用固定假响应；该修复使测试封闭，CI结果不再受开发机 `.env` 影响。
13. 新增测试23项与fixture：`test_core_contracts.py`（9）、`test_onebot_fixture.py`（5，含脱敏OneBot fixture 2份）、`test_provider_routing.py`（5）、`test_migration_t305.py`（4）。

**远程CI证据（PR #2，head提交 `df910ef`，运行 `34191586792`，结论 success）**：
- 分支：`feature/t305-neutral-contracts`（已推送并跟踪origin）。
- Pull Request：https://github.com/miaomiao636/qq-group-moderation-bot/pull/2（目标 `main`，**保持open等待主审审核**，未合并）。
- 运行总览：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34191586792
- Ubuntu质量（ruff/mypy/pytest）：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34191586792/job/101950589365 `SUCCESS`
- Windows质量（ruff/mypy/pytest）：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34191586792/job/101950589286 `SUCCESS`
- 干净运行时依赖回归：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34191586792/job/101950589387 `SUCCESS`
- 执行备注：本机直连GitHub间歇不可达，推送与API查询经本机既有代理 `127.0.0.1:7890` 完成（仅git `-c http.proxy` 单次参数与urllib代理，未修改任何git全局/仓库配置）。

**本地验证结果**：
- `uv run pytest`：**227 passed**（基线203 + 新增23；此前"1 skipped"为ffmpeg视频生成运行时skip，本轮执行成功无skip）。
- `uv run mypy app`：**54个源文件**通过（strict模式）。
- `uv run ruff check app tests alembic` / `uv run ruff format --check app tests alembic`：通过。
- Alembic完整周期：临时库 `upgrade head（b8e2f6a4c1d9）→ 写入哨兵行 → downgrade base → re-upgrade head`，路由表重建、alembic_version正确。
- 干净运行时依赖：独立venv `uv sync --locked --no-dev` 后导入 `app.main/app.core.*/官方兼容再导出/编排/流水线` 全部成功，`StandardMessage`/`ActionResult` 与再导出为同一对象，pytest不可导入。
- 核心模块静态扫描：contracts/routing/identity_backfill/orchestrator/cases.service/decision 六文件零 `from app.adapters` 顶层导入。
- 敏感信息扫描：diff与新增文件无真实AppID/AppSecret/API Key/个人QQ号；`SECRET`等命中均为既有测试占位符。

**已知风险与未完成事项**：
- **未验收**：本实现等待主审按T-305完成标准独立复核，不自行宣布验收通过。
- 旧镜像列（group_openid/member_openid等）仍保留于数据库与领域模型（expand阶段设计使然）；删除属contract阶段，需独立任务与评审。
- `app/adapters/qq_official/audit.py` 的 `log_action` 已不在编排器调用路径上，暂保留（兼容未删）；contract阶段一并清理。
- OneBot事件去重键仍是裸 `message_id`（`processed_events.provider` 列已加但未参与主键）；provider化去重键（`provider+self_id+message_id`）按T-306验收标准实现。
- `processed_events` 的provider列目前由写入方（未来T-306）填充，官方路径默认值qq_official；本任务未改dedup逻辑（保持官方行为零改动）。
- 报告构建器（`reports/service.py`）仅 `pending_manual_review` 增加中立字段输出；日报/周报计数仍读旧镜像列（双写保证一致），contract阶段切换。
- 远程CI（Ubuntu/Windows/干净运行时）在分支推送后运行，结果见下方CI小节；CI通过不等于T-305验收。
- 真实NapCat、真实MiMo评测、Windows 24×7仍全部未验收（与基线一致，未因本任务变化）。

### 历史交接

### D-019 NapCat主通道架构文档与R-104质量门禁（2026-09-08，主审Agent）

### D-019架构与任务重排（2026-09-07，主审Agent）

- 修改 `AGENTS.md`、`MEMORY_INDEX.md`、`PROJECT_CONTEXT.md`、`NEXT_TASKS.md`、`PROGRESS.md`、`DECISIONS.md`、`HANDOFF.md`、`README.md` 和 `docs/windows-operations.md`，将目标大群主通道从QQ官方机器人改为NapCat/OneBot。
- D-001被D-019替代；D-004/D-006/D-010与旧NapCat定位或Windows W1–W5相关的部分被替代；D-018保留为官方Adapter专用规则。
- 新增R-104恢复当前 `main` CI，T-305传输中立契约与expand/migrate/contract迁移，T-306 NapCat入站与影子运行器，T-307 NapCat撤回/禁言/警告Adapter；重写T-303实机验证、T-304人工批准踢人、T-403上线和T-404恢复依赖。
- 保留官方Adapter、T-001/D-012小群实测、已有审核/AI/案件/报告代码；不删除现有数据列，不将OneBot数字ID冒充成OpenID。
- 安全边界不变：踢人必须人工批准；NapCat不参与核心识别；影子模式不发出外部处罚；不实现QQ风控绕过。
- 当前 `main`/`origin/main` 均为 `88ac433`，修改前工作区干净。GitHub Actions运行 `34083954491` 实际失败：Ubuntu/Windows的 `ruff format --check` 均未通过，后续mypy与pytest被跳过；干净运行时任务成功。本地独立复现唯一未格式化文件为 `app/reports/stats.py`。
- 本轮仅文档更新，未运行全量pytest/mypy；文档完成后已执行链接、状态与关键矛盾检查，结果见本轮最终交接。

### R-104关闭与远程同步（2026-09-08，主审Agent）

- `app/reports/stats.py` 仅由ruff格式化，没有行为修改；提交 `2d8f405`。
- 本地验证：pytest 203 passed / 1 skipped；mypy 49个源文件成功；ruff check与format check成功。
- 远程验证：GitHub Actions运行 `34186194703` 中Ubuntu、Windows和干净运行时依赖三个任务全部成功。
- 此处记录的是R-104当时的历史警告；该问题已由T-405和提交 `3377279` 关闭。

### 下一位Agent注意事项（2026-09-08更新）

- T-305已通过，下一位Agent只先认领T-306；不得在T-306内实现真实处罚，也不得复用 `ACTION_MODE=OFFICIAL` 调用NapCat。
- NapCat真实验收只在Windows 10专用机、专用QQ和隔离群完成；凭据只留本机，不得写入文档或Git。
- 官方机器人实测只证明可接入小群的技术能力，不得再声称目标大群的官方上线前提已满足。

### 历史交接

### 功能分支推送与跨平台CI取证（2026-09-06，主审Agent）

- 分支：`feature/r103-ai-rule-learning`，已推送并跟踪 `origin/feature/r103-ai-rule-learning`。
- Pull Request：`https://github.com/miaomiao636/qq-group-moderation-bot/pull/1`，目标分支 `main`，**已于 2026-09-06 合并**（合并提交 `761fdba`，`main` 现含 R-103+T-105+T-204+T-205+T-106）。
- CI运行（较早，`d00960d`，运行 `34028509570`）：`https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34028509570`，结论 `success`。
- Ubuntu质量（较早 `d00960d`）：`https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34028509570/job/101473685503`，`SUCCESS`。
- Windows质量（较早 `d00960d`）：`https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34028509570/job/101473685547`，`SUCCESS`。
- 干净运行时依赖（较早 `d00960d`）：`https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34028509570/job/101473685476`，`SUCCESS`。
- CI运行（分支 tip，`27fcf6d`，运行 `34028677558`）：`https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34028677558`，结论 `success`（2026-09-06 经只读 API 直接取证）。
- Ubuntu质量（tip `27fcf6d`）：`https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34028677558/job/101474132870`，`SUCCESS`。
- Windows质量（tip `27fcf6d`）：`https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34028677558/job/101474132911`，`SUCCESS`。
- 干净运行时依赖（tip `27fcf6d`）：`https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34028677558/job/101474132949`，`SUCCESS`。
- 推送前复验：pytest、mypy、ruff check和ruff format检查全部通过。
- 结论：远程CI阻塞关闭；PR通过审核并合并后可进入W1/W2隔离群实测。CI证据不替代真实MiMo、真实QQ动作、Windows 24×7或NapCat实机验收。

### R-103/T-105/T-204/T-205/T-106 实现与本地复验（2026-09-06，主审Agent）

- 分支：`feature/r103-ai-rule-learning`。
- 本轮新增提交：
  - `d22ed4d`：新增实现计划 `docs/superpowers/plans/2026-09-06-ai-rule-learning-implementation.md`。
  - `5944e26`：R-103事件租约领取与永久失败幂等。
  - `2f65f47`：媒体存储加固与规则语境修正。
  - `69423b8`：后台状态修改统一CSRF和审计。
  - `fd994b5`：T-105版本化动态规则。
  - `da75093`：常驻引擎动态规则热刷新。
  - `562a9e3`：T-204远程AI软证据与OpenAI-compatible适配。
  - `3900479`：T-205管理员反馈与候选规则。
  - `6106260`：T-106官方动作意图与 `SHADOW/OFFICIAL` 编排。
- R-103旧阻塞已修复到自动化测试层：同事件并发领取成功数恒为1；永久契约失败重复投递不再触发唯一键异常；媒体配额、`.part`、完整SHA文件名和头部嗅探均有回归；咨询/否定/反诈教育语境不自动处罚；允许来源卡片可放行；后台POST需要登录、CSRF并记录 `AdminAudit`。
- 动态规则：全局/群级规则集、不可变版本、草稿、添加规则项、发布、回滚、审计、5秒缓存热更新和每条判定规则版本追踪已实现；候选规则只能复制到草稿，必须人工发布。
- 远程AI：实现供应商无关 `TextModerator`/`VisionModerator`，OpenAI-compatible/MiMo类适配器，按群显式启用，脱敏上传，严格JSON解析，缓存、限流、预算、用量记录和降级；单模型高置信只可把 `allow` 升为 `record_only`，不会自动撤回、禁言或踢人。
- 反馈学习：后台可对影子判定标注确认违规、确认正常、误判、未知原因撤回等；本地挖掘短语、域名和联系方式候选规则；未知撤回不当真值，AI/系统自身判定不当人工真值。
- 官方动作编排：新增 `ACTION_MODE=SHADOW/OFFICIAL` 与 `EMERGENCY_STOP`；先持久化动作意图和幂等键，再调用官方撤回、分级禁言和首次警告；数据库失败不调用外部动作；未知结果转人工，不盲目重放；任何路径都不产生踢人动作。
- 本地验证结果：
  - `uv run pytest -q`：203 passed / 1 skipped。
  - `uv run mypy app`：48个源文件通过。
  - `uv run ruff check app tests alembic`：通过。
  - `uv run ruff format --check app tests alembic`：83个文件格式检查通过。
  - 临时SQLite库Alembic：`upgrade head → current → downgrade base → upgrade head` 通过，head为 `a0b4d72e5f31`。
  - 干净运行时依赖：独立临时虚拟环境执行 `uv sync --locked --no-dev` 后可导入 `app.main` 和 `app.adapters.ai.openai_compatible`，且pytest不可导入。
- 本轮文档已同步：`.env.example`、`README.md`、`PROJECT_CONTEXT.md`、`NEXT_TASKS.md`、`MEMORY_INDEX.md`、`AGENTS.md`、`PROGRESS.md`、`HANDOFF.md`。
- 尚未完成：Pull Request #1审核与合并；真实MiMo配置与脱敏样本效果评测；W1/W2隔离群真实官方动作验证；T-404 Windows无人值守恢复；NapCat身份镜像和踢人执行器。

### 下一位Agent注意事项

- 先读取 `PROJECT_CONTEXT.md`、`NEXT_TASKS.md`、`PROGRESS.md`、`DECISIONS.md` 和本文件，不要只看历史聊天。
- Pull Request #1 已于 2026-09-06 合并（合并提交 `761fdba`）；下一步按W1/W2门槛进入隔离群动作测试（保持 `ACTION_MODE=SHADOW` 起步）。
- 若接入真实MiMo，只能在本地 `.env` 或系统凭据中配置密钥；不得把密钥、模型真实返回中的敏感内容、真实群成员身份写入源码、Markdown、测试或日志。
- 若进入W1/W2，保持 `ACTION_MODE=SHADOW` 起步；`OFFICIAL` 只可在隔离群、生产配置校验通过、负责人明确确认后启用，且仅限官方撤回/禁言/首次警告。
- NapCat仍不参与核心识别；踢人必须人工批准，真实NapCat接入另走T-303/T-304。

### R-103与AI规则学习设计（2026-09-06，主审Agent）

- 本地仓库已快进到远程 `main` 提交 `ce2f2f7`，并在分支 `feature/r103-ai-rule-learning` 进行设计记录。
- 新增设计规格 `docs/superpowers/specs/2026-09-06-ai-rule-learning-design.md`，明确分层级联AI、版本化动态规则、后台人工反馈、候选规则回放/发布和影子默认动作边界。
- 新增任务R-103、T-105、T-204、T-205、T-106；追加决策D-016至D-018；同步更新AGENTS、PROJECT_CONTEXT、MEMORY_INDEX、PROGRESS和README。
- 独立门禁：现有160项pytest通过（1项skip），mypy 41个源文件通过，ruff check和format检查通过。
- 独立复现：同一消息两个会话领取结果 `True, True`；永久解析失败第二次投递触发 `shadow_decisions.message_id` 唯一键异常；媒体目录8/10字节时仍接受5字节并增长到13；不同消息ID同尾24字符生成同名文件；三条反诈/否定/咨询文本均被判 `violation_high`。
- 本轮没有修改应用代码或数据库迁移，没有声称上述缺陷已修复；下一步必须先由负责人确认书面规格，再按R-103开始测试驱动实现。

### R-102 核心正确性整改（2026-09-06，接手Agent）

按主审10项要求执行，详见 `PROGRESS.md`。要点：
- 流水线按媒体类型分发（图片/GIF→image_engine、语音→evaluate_voice、视频→evaluate_video、文件→evaluate_file），不再全部进图片引擎；
- 去重：begin_processing 标记 PROCESSING，成功 mark_processed，失败 mark_failed 可重试（迁移 d2b1f9a60e45 加 status/error_message）；
- 媒体缺失/下载失败/解析失败 → record_only，绝不 allow（解析失败也落库一条记录）；
- 新增 app/adapters/qq_official/media.py：流式大小限制、安全文件名、磁盘配额2GB、purge_media 接入报告清理；
- GIF缓存键改完整帧 sha256；TextRuleEngine.evaluate 用 self._blacklist；ReviewGate 重做（硬证据 R001/R003，软信号 R002 不算硬证据，不重复调用）；
- 案件审计 from 赋值前捕获；案件幂等立案 + case_no 冲突重试；
- tests/test_r102.py 10项回归。全量 154+ 项测试、mypy 41文件、ruff 全过。

**R-102 审计与规则校准（2026-09-06，接手Agent + 负责人）**：
- 实测：往群发 2 条消息，影子进程正常接收并落库（id 32 image violation_high、id 33 text violation_high）。注意：当时运行的影子进程为 R-102 提交前的旧代码，实时判定不代表整改后行为；R-102 达标以 CI（Linux+Windows 全绿）为准。
- 发现 id 33（刷单广告「招小红薯评论员/一单10秒结/试做」）仅触发软信号 R002+R004，按 R-102-7 复核门应降级为 record_only。负责人裁定采用**选项A**：将强广告词（兼职/加我微信/加微/一单/秒结/评论员/试做）从弱信号提升至硬黑名单 R001，并使「命中 R001 即直接升级为 violation_high」（符合 rules.py 顶部设计注释）。
- 验收：id33 样本→violation_high（含R001硬证据，复核门放行）；仅软信号「招募」→仍 record_only（R-102-7 守住）；保护角色含黑词→record_only（不罚）。全量门禁仍绿。改动在 `app/moderation/rules.py`（提交待推送）。
- 待办：用新代码重启影子进程，方可对线上消息看到校准后判定；重启为影子模式（不处罚），安全。

**CI 实证（GitHub Actions，提交 `9c27f44`+`1e15031`，运行 `33979817730`，结论 success）**：
- 运行总览：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33979817730
- Linux 任务：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33979817730/job/101342914510
- Windows 任务：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33979817730/job/101342914699
- 运行时依赖回归（clean install）：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33979817730/job/101342914707
- 注：首版 `9c27f44` 因测试实例写 `data/media/`（CI 无此目录）导致 pytest 失败；`1e15031` 改用 `tmp_path`+monkeypatch 修复，复跑全绿。

### 影子模式上线（2026-09-05第六批，接手Agent）

- **T-403影子接入**（提交8b709c6）：`app/runtime/` runner（WS常驻/心跳/退避重连/媒体即时下载）+ pipeline（解析→去重→文字规则→复核门→媒体→落库）+ shadow_decisions表（迁移b12f6d84aa77）+ 后台影子页。141项测试全绿。
- **D-015决策**（提交5e695a7）：报告渠道=仅管理后台网页；D-014零预算方案正式认可；隐私告知=起草→负责人审定（已完成）→群内公示→正式模式。
- 运行器以独立后台进程启动（日志 `$env:TEMP\shadow_out.log`/`shadow_err.log`）；注意：该进程不受Windows Service监督（T-404范围），机器重启需手动重跑或完成T-404服务化。

### P3/P4 交付（2026-09-05第五批，接手Agent）

## 已完成内容

### P3/P4 交付（2026-09-05第五批，接手Agent）

- **T-301/T-302 管理后台与人工工作流**：`app/web/`（auth会话登录、confirm 5分钟一次性确认码、routes 案件列表/详情/证据/审批/保留/误判/取消/规则视图/报告页+清理入口）。审批流=预览（成员标注"未验证QQ号"）→生成确认码→凭码确认；全部转换经状态机（双出口互斥）。11项Web集成测试。
- **T-203 复核门与成本控制**：双通道一致性复核（无独立硬证据即拦截转人工）、CostLedger（零费用台账）、CircuitBreaker。9项测试。
- **T-401/T-402**：build_daily/build_weekly/pending_manual_review 报告构建器；purge_expired 数据保留清理（30天快照占位替换/180天日志删除）；报告页+手动清理入口。报告推送渠道与隐私告知文案待负责人确认。

### P1 审核链路交付（2026-09-05，接手Agent，负责人授权连续推进）

## 已完成内容

### P1 审核链路交付（2026-09-05，接手Agent，负责人授权连续推进）

- **T-102**（提交141f8c3）：`app/adapters/qq_official/` 六模块（contract/parser/auth/actions/dedup/audit）+ 迁移c7d2e8f91a03 + tzdata依赖；12份脱敏样本契约回归全过；动作适配器带超时/有限重试/警告不重试/保护角色错误码标记。
- **T-103**（提交8155bdd/0dc1ca0/e9a12a4）：`app/moderation/` 四模块；置信度模型可解释（黑名单0.70、弱信号0.30+0.20n、联系方式0.25起、变体+0.10、刷屏0.95）；阈值0.90；真实样本5正例全命中5反例全放行；决策结构无kick。
- **T-104**（提交36c9479）：`app/cases/` 三模块 + 迁移e8f4a1b26c57；30天窗口、两次阶梯（1h/24h禁言、第二次不警告并立案合并证据）、误判撤销联动闭案、案件状态机双出口互斥（FAILED只转人工、终态不可回退）。
- 质量门禁：91项pytest、mypy 24文件、ruff check/format 全部通过（Windows实机）。

## 已完成内容

### T-002 首批材料归档 + 媒体通道实测（2026-09-05第三轮，接手Agent）

- 负责人提供群规：允许「万能校园墙」小程序海报与正常聊天；禁止广告/色情/黄色/暴力/血腥/恐怖；不可刷屏；群主与管理员为人员白名单。
- 规则基线结构化入库 `docs/group-rules.md`（脱敏：第三方手机号/群号不写入仓库）；含对规则引擎的直接影响：二维码必须结合白名单匹配（允许海报的码与其他引流码并存）、变体规避（"薯/5一直要"、"裙"代"群"）需归一化。
- 媒体实测：升级监听器支持附件下载；8张违规例图经官方URL实时下载归档 `data/t002_media/`（gitignored，含manifest清单）；**发现URL rkey签名有时效（过期HTTP 400），必须即时下载**——决策D-013。
- 新形态确认：分享卡片以 `[卡片消息] 小程序` 文本到达（含source/摘要/source_logo）；语音附件含 `asr_refer_text` 官方转写与 `voice_wav_url`——均写入D-013。
- 新增脱敏样本3份（share_card/text_spam×2），fixture累计12份。
- 执行备注：首轮下载脚本文件名冲突导致覆盖，已用事件内URL补下载恢复全部8张（无需用户重发）。

### T-001 QQ官方能力验证·核心实测（2026-09-05第二轮，接手Agent）

- 前置就位：负责人已将机器人设为测试群管理员，并提供普通成员测试号（member_role=member）。
- 撤回实测：`DELETE /v2/groups/{group_openid}/messages/{message_id}` 对普通成员消息返回 HTTP 200；**对同一消息重复撤回再次返回200——接口幂等**，动作层重试可直接依赖。
- 禁言实测：`POST /v2/groups/{group_openid}/restrict_chat_setting`，3600秒与86400秒（`mute_expire_at` RFC3339到期时间）均返回200；`op=del`+空到期时间解除禁言返回200。
- 保护角色负面用例：尝试禁言群主返回 HTTP 400 `40103004「目标成员为机器人/群主/管理员，不允许被禁言」`——平台层硬限制；业务层仍需自行拦截白名单普通成员（平台不保护）。
- 媒体样本采集（监听窗口实测收到）：GIF（image/gif，content含faceType=6标记）、语音（voice，.amr）、视频（video/mp4）、文件（file，.pdf）、转发记录（content为`[群聊的聊天记录]`文本骨架，630字符）。附件结构含 `url`（可下载）与 width/height。
- 脱敏入库：`tests/fixtures/qq_official/` 新增5份（gif/voice/video/forward_record/file_pdf），累计9份；文件名遮蔽保留扩展名、转发记录逐行脱敏、附件URL遮蔽。
- 结论归档：全部实测结论写入 `DECISIONS.md` 决策 **D-012**。
- 执行方式备注：监听/撤回/禁言均为系统临时目录一次性验证脚本（未入仓库），实测后目标测试号的禁言已解除、消息已撤回，未在测试群遗留副作用。

### T-001 QQ官方能力验证·第一阶段（2026-09-05，接手Agent）

- 外部资源到位：负责人创建机器人应用（AppID 1905561634，机器人UIN 4017145414）与4个隔离测试群，机器人已入群并被授权查看消息；凭据由负责人提供后仅写入本机 `.env`（已在 `.gitignore`），未入仓库与文档。
- 连通性：`api.bot.qq.com/app/getAppAccessToken` 签发成功。**排障记录**：先用旧域名 `api.sgroup.qq.com` + `Authorization: QQey` 前缀，`/users/@me` 与 `/gateway` 均返回401（空错误体）；改用现行文档统一域名 `api.bot.qq.com` + `QQBot` 前缀后全部通过。后续适配器必须使用新域名与新前缀。
- 身份与网关：`GET /users/@me` 200（机器人ID/头像/share_url 正常）；`GET /gateway` 200，返回 `wss://api.sgroup.qq.com/websocket`。
- 全量消息事件：临时 WebSocket 监听器（intents=1<<25，系统临时目录脚本，未入仓库）收到4条 `GROUP_MESSAGE_CREATE` 事件，其中包含**不带@的普通文字消息**，证明全量消息事件已生效；覆盖文字@（`<@member_openid>` 标记+mentions）、普通文字、表情（`<faceType=...>`）、图片（attachments 含 content_type/size 元数据，无content）。
- 关键发现：事件中的 `group_id` 字段是32位不透明十六进制串，**不是真实数字群号**；作者字段含 `member_role`（实测群主为 `owner`、机器人自身在mentions中为 `member`）。**OpenID↔数字QQ映射风险结论维持不变**，人工客户端/NapCat回退设计不变。
- 撤回实测：`DELETE /v2/groups/{group_openid}/messages/{message_id}` 返回 HTTP 400 `{"code":40062003,"message":"无操作权限"}`——机器人当前 member_role=member；且被撤回对象为群主消息（群主消息可能不可撤）。需群主将机器人设为群管理员，并准备普通成员测试号后复测。
- 禁言接口定义确认（与"mute_seconds"旧假设不同）：`POST /v2/groups/{group_openid}/restrict_chat_setting`，请求体 `members` 数组（单批≤20），元素含 `op`（add/update/del）、`member_openid`、`mute_expire_at`（RFC3339到期时间）；最长30天；**不能禁言群主/管理员/机器人本身**；解除禁言用 `op=del` + 空 `mute_expire_at`。1小时禁言=当前时间+3600s 的 RFC3339 时间戳。尚未实测。
- 脱敏样本：4份入库 `tests/fixtures/qq_official/`（text_at/text_plain/face/image），ID遮蔽保留长度与前后缀、消息ID保留 `ROBOT1.0_` 前缀、用户名与正文替换为占位符、message_scene.ext 令牌遮蔽、机器人自身昵称与成员角色保留；经抽查无真实内容泄漏。

### W0独立复验（接手Agent，2026-09-04，本机=正式Windows 10专业版测试机）

按AGENTS.md"先检查实际文件、运行结果和测试状态，再相信文档"的要求，本轮接手Agent未直接采信前一轮W0文档记录，而是在同一台Windows 10专业版测试机上实际重跑全部W0门禁，结果全部通过：

- 系统信息复核（与决策D-011记录一致）：Windows 10专业版 22H2，Build **19045.6466**（CurrentBuild 19045 / UBR 6466，BuildLabEx 19041.1.amd64fre）；架构 **AMD64**；最新补丁KB5071982/KB5071959/KB5072653（安全更新，2026-07-18）、KB5066130/KB5066790（2026-07-16）。
- 干净安装：`uv sync --all-groups --reinstall` 强制按锁文件重装全部包（Resolved 48 / Prepared & Installed 46，exit 0），等效于全新环境安装。
- 迁移：`alembic current`（head）→ `downgrade base` → `upgrade head` → `current` 完整迁移周期通过，head版本 `3a9c0c662c2e`（init system_meta）。
- 质量门禁：`uv run pytest` **24 passed**（24.59s）；`uv run mypy app` Success: no issues found in 7 source files；`uv run ruff check app tests alembic` All checks passed!（exit 0）；`uv run ruff format --check app tests alembic` 13 files already formatted（exit 0）。
- 实际端口：`WEB_PORT=8135` 启动后 `GET /healthz` 在8135端口返回 HTTP 200，body `{"status":"ok","env":"local","mode":"SAFE"}`；默认端口8000启动后同样返回 HTTP 200。
- 密钥边界：全程未创建 `.env`，未配置任何QQ AppID/AppSecret、NapCat地址或模型API Key，符合W0定义。
- 执行环境备注：①本轮因审批超时未手工删除 `.venv`/`data/`，改用 `--reinstall` 实现锁文件级干净重装、以"降级到base再升级到head"完整周期替代全新库迁移，验证力度等价；②ruff 运行时出现 `.ruff_cache` 写入 `拒绝访问 (os error 5)` 警告但检查结果与退出码不受影响，属执行环境权限特性，非代码问题。

### W0 Windows基础兼容门禁（2026-09-04，实机通过）

- 测试机系统记录（按决策D-011）：Windows 10专业版 22H2，Build **19045.6466**；CPU 12th Gen Intel(R) Core(TM) i5-12400，架构 **AMD64**；内存15.7GB；磁盘C: 149.3GB（余79.4）/ D: 781.5GB（余727.8）/ E: 465.8GB；网络为有线以太网，Realtek Gaming 2.5GbE网卡（链路1Gbps）；最新补丁KB5071982/KB5071959/KB5072653（安全更新，2026-07-18）、KB5066130（更新）与KB5066790（安全更新，2026-07-16）；系统安装于2026-07-12，最近启动2026-07-19。
- 代码获取：该机通过Git Credential Manager凭据克隆私有仓库 `miaomiao636/qq-group-moderation-bot`（main @ `c22b0c1`，工作区干净），克隆过程未要求交互认证。
- 干净安装：`uv sync --all-groups` 创建 `.venv`（CPython 3.12.10），Resolved 48 / Installed 46 个包，exit 0。
- 迁移：`uv run alembic upgrade head` 成功执行 `Running upgrade -> 3a9c0c662c2e, init system_meta`（SQLite），exit 0。
- 质量门禁：`uv run pytest` **24 passed**（25.28s）；`uv run mypy app` Success: no issues found in 7 source files；`uv run ruff check app tests alembic` All checks passed；`uv run ruff format --check app tests alembic` 13 files already formatted。
- 实际端口：`WEB_PORT=8135` 启动后 `/healthz` 在8135端口返回200，uvicorn日志确认监听 `http://127.0.0.1:8135`。
- 配置拒绝：非法 `LOG_LEVEL=BOGUS`、越界 `WEB_PORT=99999`、Windows盘符相对路径 `DATABASE_URL=sqlite+aiosqlite:///C:relative\blocked.db` 三类非法配置均被拒绝启动（ValidationError，错误信息明确可操作）。
- 健康检查：默认8000与自定义8135端口均返回 `{"status":"ok","env":"local","mode":"SAFE"}`。
- 约束遵守：全程未配置任何QQ AppID/AppSecret、NapCat地址、模型API Key；`.env` 未创建，应用以SQLite默认配置（`APP_ENV=local`、`RUN_MODE=SAFE`）启动。
- 执行备注：该机非交互PowerShell以GBK编码解析命令，中文字面量路径会被误读导致解析失败；本次全部改用相对路径与通配符解析规避，属执行环境特性，不影响项目代码。
- 遗留提示：本轮W0在该机新克隆仓库执行（机器非完全空白，已预装开发工具与Node.js/FFmpeg等多媒体组件），符合PROJECT_CONTEXT中"R-101后先验证基础安装和Windows兼容性"的阶段定义；本机文档变更尚未提交git。

### Windows 10专业版测试机就绪确认（决策D-011，2026-09-04）

- 项目负责人确认正式整机测试机为Windows 10专业版；该机已通过CodeBuddy安装Node.js LTS、Git、Python 3.12、FFmpeg与uv。
- 新增决策D-011：替代D-010中“优先Windows 11 x64”的初始假设；代码保持跨平台通用，不做Windows专用分支。
- 同步更正 `PROJECT_CONTEXT.md`、`docs/windows-operations.md`、`AGENTS.md`、`NEXT_TASKS.md`、`PROGRESS.md`、`MEMORY_INDEX.md` 中“优先Windows 11 x64”表述。
- W0状态由“阻塞（无电脑）”转为“测试机已就绪、门禁待执行”；门禁真实通过前不得宣称W0完成。
- 本轮仅为文档状态更新，未修改任何代码；无测试可执行，验证方式为文档一致性核查。

### 首轮整改（R-101）

- **整改项1**：将 `aiosqlite` 从开发依赖移入运行时依赖；干净生产环境（不带 dev 组）可导入 `app.main` 并启动。
- **整改项2**：取消 `Base.metadata.create_all`；新增 `check_db_migrated()` 校验数据库已通过 Alembic 迁移，未迁移则拒绝启动。
- **整改项3**：为 `APP_ENV`/`RUN_MODE`/`WEB_PORT`/保留天数/`LOG_LEVEL` 增加类型与范围校验；生产环境拒绝空管理员密码。
- **整改项4**：新增 `app/__main__.py` 启动入口，读取 `WEB_HOST`/`WEB_PORT`；`uv run python -m app` 启动时端口生效。
- **整改项5**：将 `alembic/` 纳入 ruff 检查与格式检查，修复迁移文件尾随空格与导入顺序问题。
- **整改项6**：CI 使用 `uv sync --locked` 锁文件安装，新增 Windows 测试环境（Linux + Windows 矩阵）。
- **整改项7**：测试数据库改用 `tempfile.mkdtemp` 临时目录隔离，不再使用固定 `tests/test_data/test.db`。
- **整改项8**：修正 README 目录结构，区分"当前实际存在"与"规划中"目录。
- **整改项9**：安装 `httpx2` 解决 TestClient 的 httpx 弃用警告；锁定 `anyio` 内部警告；修复 Alembic `path_separator` 警告。
- **整改项10**：初始化 Git 仓库，建立整改前基线提交 `688a5da`。

### 复验整改（R-101 复验项）

- **复验项1**：`check_db_migrated()` 现在校验数据库版本必须等于当前代码的 Alembic head（`3a9c0c662c2e`），拒绝 `stale_revision` 等过期版本，防止旧数据库结构直接运行新代码。
- **复验项2**：生产环境密码使用 `strip()` 后校验，仅含空白字符的密码被拒绝。
- **复验项3**：CI 新增 `runtime-deps` 回归检查 job，仅安装运行时依赖并验证 `import app.main`，防止运行时依赖被误放入开发组。
- **复验项4**：Windows CI 已配置 Linux+Windows 矩阵，但当前仓库无远程地址，无法在 GitHub Actions 产生 Windows 真实运行证据；需推送远程仓库后由主审Agent确认。
- **复验项5**：测试临时目录在会话结束后主动删除（`shutil.rmtree`），不再残留 `qqbot-test-*`/`qqbot-nomigrate-*`。

### 二轮复验整改（R-101 复验项 6-8）

- **复验项6**：修复运行时依赖 CI 失效。`uv run` 默认会自动重新同步项目环境（含 dev 组），导致 `uv sync --no-dev` 后 dev 依赖被悄悄装回。改用 `uv run --no-sync` 阻止自动重装，并新增反向断言：dev 依赖（pytest）在干净运行时环境中必须不可导入。
- **复验项7**：修复非项目工作目录无法启动。`get_head_revision()` 原用相对路径 `Config("alembic.ini")`，切换工作目录后报 `No 'script_location' key found`。现基于 `PROJECT_ROOT` 解析 `alembic.ini`，并将 `script_location` 设为项目根目录的绝对路径，从任意工作目录启动均可定位迁移脚本。
- **复验项8**：修复 Windows 清理风险。删除测试临时目录前先关闭全局数据库引擎（`engine.dispose()`），避免 Windows 上 SQLite 文件被占用无法删除；移除 `ignore_errors=True`，删除失败显式暴露，不再隐藏。

### 三轮复验整改（R-101 复验项 9-10）

- **复验项9**：修复相对SQLite路径依赖当前工作目录。`app/config.py` 新增 `_normalize_sqlite_url`，在配置层把相对路径（含 `./` 与不含 `./`）统一解析到 `PROJECT_ROOT` 下；绝对路径（Unix/Windows 正反斜杠）与 `:memory:` 保持不变。`Settings` 加载时自动规范化，迁移与启动从任意工作目录连接同一数据库。
- **复验项10**：新增 `tests/test_sqlite_path.py` 回归测试，覆盖 README 默认配置、非项目工作目录启动、Windows 绝对路径（正/反斜杠）、Unix 绝对路径、`:memory:`、非 SQLite URL，以及迁移使用规范化绝对路径的端到端校验。

### 四轮复验整改（提交 `7fca851` 主审未通过后的整改）

- **整改A（测试安全）**：三轮版本的 `tests/test_sqlite_path.py` 会删除真实 `PROJECT_ROOT/data/moderation.db`，属于危险测试。现已重写为完全使用 pytest `tmp_path`，测试代码不再出现任何对项目数据目录的写/删操作；新增模块级守卫夹具 `_guard_real_data_dir`，对真实数据目录做前后内容快照（文件名+SHA256），被触碰即断言失败；端到端测试预创建合法 SQLite 哨兵库（含哨兵表），验证迁移不删除、不替换预存在数据库。
- **整改B（端到端执行真实 Alembic）**：三轮版本的"端到端迁移测试"未执行 Alembic。现已修复 `alembic.ini` 相对路径问题：`script_location = %(here)s/alembic`、`prepend_sys_path = %(here)s`，使 Alembic CLI 可从任意工作目录执行。新增子进程测试：从非项目目录执行真实 `alembic upgrade head`（校验 alembic_version 等于 head），再从另一个非项目目录以子进程启动应用并轮询 `/healthz`，确认连接同一数据库；另含未迁移空库拒绝启动的子进程测试。
- **整改C（临时目录）**：`tempfile.mkdtemp(prefix="qqbot-cwd-")` 改为 pytest `tmp_path`，测试后无 `qqbot-cwd-*` 残留。
- **整改D（Windows盘符相对路径）**：`C:relative\db.db` 这类盘符相对路径依赖各盘符的当前工作目录，不可靠。`_normalize_sqlite_url` 现在明确拒绝该形式并给出可操作的错误信息；`C:/...` 与 `C:\...` 绝对路径仍原样保留。
- **整改E（文档状态）**：修正 `PROGRESS.md`、`HANDOFF.md`、`NEXT_TASKS.md`，不再表述"仅剩CI证据"；如实记录主审未通过与整改范围。
- **整改F（完整验证）**：在干净临时副本（含哨兵 `data/moderation.db`）中运行全部验证，详见"验证结果"。
- **整改G（远程CI证据）**：创建私有远程仓库 `miaomiao636/qq-group-moderation-bot` 并推送。首次真实Windows CI暴露 `alembic.ini` 中文注释在cp1252编码下解码失败的问题，已修复（ini改为ASCII注释 + CI强制 `PYTHONUTF8=1`）。提交 `0e0dd73` 的三个CI任务全部真实成功，详见"验证结果"。

### Windows CI 首次真实运行暴露并修复的问题

- Windows runner 默认 locale 为 cp1252，configparser 按 locale 编码读取含中文注释（UTF-8字节）的 `alembic.ini`，`UnicodeDecodeError` 导致所有测试 setup 失败。
- 修复：`alembic.ini` 注释改为 ASCII；CI quality 任务设置 `PYTHONUTF8=1`。该修复同时保护 Windows 生产部署时 `get_head_revision()` 读取 `alembic.ini` 的路径。

## 修改文件

- 本轮（T-002归档+媒体实测）修改：`DECISIONS.md`（新增D-013）、`NEXT_TASKS.md`、`PROGRESS.md`、`HANDOFF.md`、`MEMORY_INDEX.md`；新增 `docs/group-rules.md`、`tests/fixtures/qq_official/` 3份样本（累计12份）；本机归档 `data/t002_media/`（8张违规例图+manifest，gitignored）。未修改任何应用代码。
- 前轮（T-001第一阶段）修改：`NEXT_TASKS.md`、`PROGRESS.md`、`HANDOFF.md`；新增 `tests/fixtures/qq_official/`（4份脱敏样本）。未修改任何应用代码。
- 前轮（W0交接）修改：`PROGRESS.md`、`NEXT_TASKS.md`、`HANDOFF.md`、`MEMORY_INDEX.md`、`PROJECT_CONTEXT.md`（仅状态与证据记录，未修改任何代码与测试）。
- 新增：`app/__main__.py`、`tests/test_sqlite_path.py`。
- 修改：`pyproject.toml`、`uv.lock`、`app/config.py`、`app/db.py`、`app/main.py`、`tests/conftest.py`、`tests/test_health.py`、`alembic/env.py`、`alembic/script.py.mako`、`alembic/versions/3a9c0c662c2e_init_system_meta.py`、`alembic.ini`、`.github/workflows/ci.yml`、`.env.example`、`.gitignore`、`README.md`、`AGENTS.md`、`DECISIONS.md`、`PROGRESS.md`、`NEXT_TASKS.md`、`HANDOFF.md`、`docs/windows-operations.md`。

## 验证结果

### W0 实机验证证据（2026-09-04，执行机=正式Windows 10专业版测试机）

- `uv sync --all-groups`：exit 0，46包安装（运行时+开发组，CPython 3.12.10）。
- `uv run alembic upgrade head`：exit 0，`Running upgrade -> 3a9c0c662c2e, init system_meta`（SQLite）。
- `uv run pytest`：24 passed in 25.28s。
- `uv run mypy app`：Success: no issues found in 7 source files。
- `uv run ruff check app tests alembic`：All checks passed!（exit 0）。
- `uv run ruff format --check app tests alembic`：13 files already formatted（exit 0）。
- 实际端口：`WEB_PORT=8135` 下 `GET http://localhost:8135/healthz` → HTTP 200，body `{"status":"ok","env":"local","mode":"SAFE"}`；日志确认监听 `http://127.0.0.1:8135`。
- 配置拒绝：`LOG_LEVEL=BOGUS`、`WEB_PORT=99999`、`DATABASE_URL=sqlite+aiosqlite:///C:relative\blocked.db` 三类非法配置进程均快速退出并抛出 ValidationError（分别为非法日志级别可选值提示、端口上限65535校验、盘符相对路径明确拒绝并给出修正建议）。
- 默认端口健康检查：`GET http://localhost:8000/healthz` → HTTP 200。
- 密钥边界：未创建 `.env`，未配置任何QQ或模型密钥，符合W0定义。
- 干净安装说明：测试机为新克隆仓库 + 全新 `.venv` 安装；系统级工具（Node/Git/Python/uv/FFmpeg）此前已装（决策D-011），与W0门禁无关，W0以门禁命令真实通过为准。

- 干净生产依赖安装（仅运行时）：`import app.main` 成功，`aiosqlite` 已安装，`pytest` 不在运行时环境。
- 干净生产环境启动：`uv run python -m app` 启动成功，健康检查返回 `{"status":"ok","env":"local","mode":"SAFE"}`。
- `uv run pytest`：11 passed，无警告。
- `uv run mypy app`：Success, no issues found in 7 source files。
- `uv run ruff check app tests alembic`：All checks passed。
- `uv run ruff format --check app tests alembic`：12 files already formatted。
- Alembic 全新数据库升级：成功，`alembic_version` 版本为 `3a9c0c662c2e`。
- Alembic 降级后重新升级：成功。
- 未迁移数据库启动：被拒绝，抛出 `RuntimeError: 数据库未通过 Alembic 迁移`。
- **过期版本拒绝**：`stale_revision` 被拒绝，错误信息提示需 `alembic upgrade head`。
- **head 版本匹配**：迁移到 head 的数据库通过 `check_db_migrated()`。
- **空白密码拒绝**：生产环境 `ADMIN_PASSWORD="   "` 被拒绝；正常密码通过。
- 无效配置拒绝启动：非法 `RUN_MODE`/越界端口/负数保留期/非法日志级别/生产空密码均被拒绝。
- `WEB_PORT` 实际生效：`WEB_PORT=8125`/`8127` 启动后健康检查在对应端口返回成功。
- CI 配置：YAML 语法正确，`quality`（Linux+Windows 矩阵）+ `runtime-deps` 两个 job。
- `uv sync --locked --all-groups`：通过。
- `uv build`：源码包和 wheel 构建成功。
- 敏感信息扫描：无泄漏。
- 测试临时目录清理：测试后无残留 `qqbot-test-*`/`qqbot-nomigrate-*` 目录。
- **运行时依赖 CI 回归**：干净 `uv sync --no-dev` 后，`uv run --no-sync` 导入 `app.main` 成功，pytest 不可导入（dev 依赖未装回）。
- **非项目工作目录启动**：从 `/tmp` 调用 `get_head_revision()` 返回 `3a9c0c662c2e`；从 `/tmp` 启动应用健康检查返回 `{"status":"ok","env":"local","mode":"SAFE"}`。
- **Windows 清理**：测试后临时目录被主动删除，删除前关闭全局数据库引擎，无 `ignore_errors` 隐藏。
- **SQLite相对路径**：`_normalize_sqlite_url` 将 README 默认 `sqlite+aiosqlite:///./data/moderation.db` 解析为 `PROJECT_ROOT/data/moderation.db`；从非项目工作目录解析结果一致；Windows 绝对路径（正/反斜杠）、Unix 绝对路径、`:memory:`、非 SQLite URL 均保持不变；Windows 盘符相对路径 `C:relative\db.db` 被明确拒绝。
- **SQLite路径回归测试**：`tests/test_sqlite_path.py` 13项全部通过；`uv run pytest` 共 24 passed。

### 四轮整改验证（在干净临时副本中执行，副本含哨兵 `data/moderation.db`）

- **测试安全**：24 项 pytest 全部通过；测试前后哨兵数据库 SHA256 完全一致（`48d998f0d8a7c370`），哨兵表保留，证明测试未触碰、未删除、未替换真实数据目录文件。
- **临时目录**：测试后无 `qqbot-cwd-*`、`qqbot-test-*` 残留。
- **静态检查**：ruff check 与 format、mypy（7 个源文件）全部通过。
- **Alembic 升降级**：全新库升级到 head、降级到 base、再升级到 head 均成功。
- **构建**：`uv build` 源码包和 wheel 构建成功。
- **配置拒绝**：生产空白密码、非法日志级别、Windows 盘符相对路径均被拒绝启动。
- **实际端口**：`WEB_PORT=8133` 启动后 `/healthz` 在该端口返回成功。
- **运行时依赖**：干净 `uv sync --no-dev` 后 `uv run --no-sync` 导入 `app.main` 成功，pytest 不可导入。
- **Git**：基线提交 `688a5da` 已建立，修复修改可审计。

### 远程 CI 真实运行证据（复验项11，提交 `0e0dd73`）

- 远程仓库：`https://github.com/miaomiao636/qq-group-moderation-bot`（私有）。
- CI 运行：`https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33877462326`（结论 success，head 提交 `0e0dd73`）。
- Ubuntu质量：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33877462326/job/101037748579 ✓
- Windows质量：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33877462326/job/101037748749 ✓
- 运行时依赖回归：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33877462326/job/101037748778 ✓

## 遗留问题

- **T-002 待补（持续，不阻塞开发）**：各类别样本距离验收数量（≥20正例+30反例，媒体各≥10）差距大，负责人持续提供。
- **决策D-014已定**：不采购内容安全服务商（负责人零预算确认）；色情/暴力类=平台风控兜底+文字规则+可选本地开源分类器+人工复核；该类别召回率不承诺；日报需含"仅记录待人工"列表。
- **T-001 剩余项（低风险）**：限流触发阈值与限流响应格式未实测（不主动刷限流）；群主消息撤回行为未测（非计划依赖能力）。
- **凭据安全提示**：AppSecret 曾出现在聊天记录中，建议 T-001 收尾后由负责人在开放平台重置一次，并改由负责人直接维护 `.env`。
- **凭据安全提示**：AppSecret 曾出现在聊天记录中，建议 T-001 收尾后由负责人在开放平台重置一次，并改由负责人直接维护 `.env`。
- **R-101已通过**：四轮整改与远程CI取证（复验项11）均已完成并获主审确认，无遗留阻塞项。
- **W0 已通过（2026-09-04）且经接手Agent同日独立复验通过**：Windows基线已建立，两轮证据见"验证结果"W0小节。遗留提示：首轮在该机新克隆仓库执行（机器非完全空白，已装开发工具），符合"R-101后先验证基础安装和Windows兼容性"阶段定义；文档变更已随本轮提交入库。
- **T-001 阻塞**：需要项目负责人提供QQ官方应用、隔离测试群与全量消息/撤回/禁言权限。
- **T-002 阻塞**：需要项目负责人提供群规、白名单与脱敏样本。
- Windows 真实运行、自启、重启和更新恢复需在 Windows 专用机通过 T-404 演练验证，当前 Mac 环境无法验证。
- T-404 只有文档和验收标准，尚无实际实现与实机证据。

## 下一步建议

1. （已完成，2026-09-04）W0门禁已在Windows 10专业版测试机实机通过；接手Agent同日独立重跑全部门禁亦全部通过（见"W0独立复验"小节），本轮文档变更已提交入库。
2. T-001所需机器人应用/隔离群已就位；请项目负责人：①在测试群将机器人设为群管理员；②准备普通成员测试号发消息供撤回/禁言实测；③继续提供 T-002 所需群规/白名单/脱敏样本。事件结构已实测确认，T-102 的消息契约与适配器设计可并行启动。
3. 完整Windows阶段和门槛见 `docs/windows-operations.md`；W1（QQ官方链路）在T-001与T-102通过后开始。

## 主审复验结论

### 对提交 `8f3ea0f` 的复验

- 已通过：`uv sync --locked --all-groups`、11项pytest、mypy、ruff检查与格式、构建、锁文件、依赖兼容、干净运行时依赖、Alembic升级/降级/head校验、配置拒绝、实际 `WEB_PORT`、不在项目目录时的Alembic脚本定位。
- 未通过：README默认相对SQLite配置的目录稳定性；远程Linux/Windows CI真实运行。
- 决定：R-101不通过，T-102暂不放行。

### 对提交 `7fca851` 的复验

- 未通过，理由：
  1. `tests/test_sqlite_path.py` 会删除真实 `PROJECT_ROOT/data/moderation.db`，属于危险测试。
  2. 所谓端到端迁移测试没有执行 Alembic，验证力度不足。
  3. 测试使用 `tempfile.mkdtemp(prefix="qqbot-cwd-")`，存在残留风险。
  4. Windows 盘符相对路径 `C:relative\db.db` 未处理（既未规范化也未拒绝）。
  5. 文档错误表述"仅剩CI证据"。
- 决定：R-101继续不通过，T-102暂不放行。

### 四轮整改后状态（R-101已通过）

- 提交 `7fca851` 复验提出的缺陷已全部整改：测试完全使用 `tmp_path` 并带真实数据目录守卫夹具；`alembic.ini` 使用 `%(here)s` 并新增子进程真实 Alembic 升级与跨目录启动的端到端测试；盘符相对路径明确拒绝；文档状态已修正。
- 干净临时副本（含哨兵真实数据库）中完成全部验证：24项pytest、静态检查、Alembic升降级、构建、配置拒绝、实际端口、运行时依赖；哨兵数据库字节级未变。
- 复验项11已完成：私有远程仓库已建立，提交 `0e0dd73` 的 Ubuntu质量、Windows质量、运行时依赖三个CI任务真实成功（链接见"验证结果"）。
- **主审复验结论：R-101通过。**
