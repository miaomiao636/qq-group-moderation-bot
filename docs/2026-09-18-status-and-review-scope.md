# 项目现状与待主审复核清单（2026-09-18，本轮 r132）

> 用途：交接给主审的**本轮复核请求**。本轮改动**已部署并在生产运行**（三次提权重启：
> 16:24 / 17:06 / 17:18），按项目惯例**部署后送主审独立复验**；若主审判定存在 P0/P1，
> 请按既有流程回退或整改（回滚手册见 `docs/deploy-runbook-d037-d038.md`）。

## 一、提交与基线

| 项 | 值 |
|---|---|
| 审查基线（主审上一轮） | `main@68a94b9`（r127 P1 关闭，PR #44）；其上为 `fb7817d`（r130 P1 修复）、`eccdfee`（r131 负责人 UI 调整） |
| 本轮提交 | 分支 `windows-deploy-2026-09-10` 的**工作树改动**（`756e4d2` 之上）：25 个修改文件（+1490/−84）+ 7 个新增文件 |
| 生产提示词 | `t204-v15`（本轮由 `t204-v14` 升级）；`config/ai_prompt_rules.txt` SHA256 `45B87843736FA4F4CC96C7D59D32576021407F2A8819EB5AA5471A5096299D9E` |
| 数据库迁移 | 新增 `c9a1f4d27e30`（`allowlist_members`）；生产 `alembic current == head == c9a1f4d27e30` |
| 本地门禁 | 全量 **1374 passed / 15 skipped / 0 failed**；`ruff check` + `ruff format --check`（232 文件）、`mypy`（88 源文件）通过 |

## 二、本轮变更汇总（按决策）

### D-037 成员白名单（按 QQ 号）——全类别完全放行 + 文件批量同步（2026-09-18）

- **政策**：负责人在知悉风险后明确选择「**不守 B-2 底线**」——名单内成员**全类别完全放行**
  （含诈骗/色情/暴力/刷屏）：不撤回、不处罚、不转人工；风险（账号被盗/转手 = 完全敞开）已书面告知。
- **身份与匹配**：新表 `allowlist_members`（`provider` + `external_user_id`，唯一约束，`sqlite_autoincrement`），
  **精确相等**匹配（NapCat 主通道即数字 QQ 号）；**不复用**关键词白名单的变体归一化；官方通道 openid 不适用。
- **优先级**：成员白名单 → 保护角色（群主/管理员）→ 关键词白名单（D-033）→ 常规规则。
- **全链路保护**：新政策标记 `POLICY_ALLOWLIST_MEMBER_ALLOW` 纳入 `POLICY_ALLOW_RULE_IDS`，
  并进入新的 `FULL_ALLOW_NO_UPGRADE_RULE_IDS`（AI 二审、显式 DR、媒体层**一律不得升级或转人工**）。
- **运行时**：每消息 fresh 直读（跨进程、保存即生效）；读失败返回**空集**（fail-closed）。
- **后台**（`app/web/routes.py`）：单条增删启停 + **文件全量同步**（上传 → 解析 → 差异预览 → 确认 → 写入 + `AdminAudit`）
  + 导出；空文件拒绝；停用超 **5 条且 >30%** 时需二次确认（`MemberSyncPlan.needs_confirm`）。
  文件格式：纯文本，每行一个 QQ 号，`#` 注释，支持 `QQ号,备注`。
- **文件**：`app/models.py`、`app/moderation/allowlist.py`（+318）、`app/moderation/allowlist_members_io.py`（新）、
  `app/moderation/decision.py`、`app/moderation/rules.py`、`app/runtime/pipeline.py`、`app/web/routes.py`、
  `alembic/versions/c9a1f4d27e30_add_allowlist_members.py`（新）、`tests/test_r116_member_allowlist.py`（新，607 行）。

### D-038 合并转发与群名片一律撤回（群主/管理员/成员白名单除外）（2026-09-18）

- **口径**：两条**结构性确定性规则**——`R_FORWARD_RECORD`（合并转发）、`R_GROUP_CARD`（群名片）
  → `violation_high` + 标准动作集；**不调用** `get_forward_msg` 展开内容（负责人只要求"一律撤回"）。
  保护角色与成员白名单在上游分支排除。
- **群名片识别**：`ShareCardInfo.is_group_card` 新增；`parser.py` 多信号宽容判定
  （`meta.group` / app 含 group|qun / view==group / 文案含"群名片|推荐群"）——**尚无真实样本验证字段结构**（见第五节）。
- **兜底调整**：`_contains_unreviewable_content` 不再把 `forward_record` 降级转人工（未知消息段仍转人工）。
- **复核门**：`_HARD_EVIDENCE_RULES` 改为「内置三项（`R001/R003/R006`）+ `decision.py` 导出的结构性常量」。
  ⚠️ **含一次事故修复**：结构性规则最初误编为 `R007`，与内置 `contextual_ad_terms` **撞号**并被一并加入硬证据集合
  → 复核门把内置规则当独立硬证据、**放宽了自动处罚门槛**。实测影响 **3 条**（均"仅 R007、无其它硬证据"），
  **全部落在急停窗口内、动作 SKIPPED、真实撤回 0 次**。修复后编号改为语义前缀；新增 2 条回归
  （编号不得匹配 `R0\d\d`、硬证据集合不得含 `R002/R004/R005/R007`）。
- **文件**：`app/moderation/rules.py`、`app/moderation/decision.py`、`app/moderation/review_gate.py`、
  `app/adapters/onebot/parser.py`、`app/core/contracts.py`、`app/runtime/pipeline.py`；
  回归更新 `tests/test_moderation_rules.py`、`tests/test_onebot_ws.py`。

### D-039 图片含微信小程序二维码 → 一律通过（2026-09-18）

- **触发**：负责人反馈"带小程序码的图被撤回"。只读诊断见
  `docs/review-2026-09-18-image-exemption-failure.md`。
- **实现**：视觉模型新增**严格布尔**字段 `has_miniprogram_code`（仅 `source=vision` 有效，非布尔一律拒收、不猜）
  → `_miniprogram_qr_allow()` 命中即 `allow` + 政策标记 `POLICY_MINIPROGRAM_QR_ALLOW`
  （**刻意不加入** `POLICY_ALLOW_RULE_IDS`——该集合语义是"全类别完全放行"，本政策例外必须在合并层判断）。
- **例外（当日深夜负责人二次修订）**：由「诈骗/色情/暴力」**收窄为「色情 / 暴力违禁品」**
  （`_MINIPROGRAM_QR_BLOCKED_CATEGORIES`）——**诈骗不再例外**；提示词同步改为"仅两条例外，诈骗线索写入
  evidence 供人工抽查"。保留的第二类例外：**本地硬证据**（`R001`/`R003`/`R006`/`DR_`）不被图片外观覆盖。
- **提示词**：新增规则 + 明确「QQ 群二维码 / 个人名片码 / 普通链接二维码 / 条形码**不是**小程序码」；
  `PROMPT_VERSION` `t204-v14` → **`t204-v15`**（`app/moderation/ai.py` + `.env` 同步）。
- **线上只读核查（4000 条判定逐条解析 JSON）**：只要视觉模型置 `true` 即**全部放行、0 例外**；
  被撤记录中命中 `POLICY_MINIPROGRAM_QR_ALLOW` 的 **0 条**；模型自述"小程序码通过"的 8 条与字段值完全一致。
- **视觉探针**：负责人提供的两张已撤原图，用生产同款配置实测均返回 `category=None / confidence=1.0 /
  needs_review=False / has_miniprogram_code=True` → 当前口径下**不会**再被撤回。
- **文件**：`app/moderation/ai.py`、`app/moderation/decision.py`、`config/ai_prompt_rules.txt`、
  `tests/test_r116_miniprogram_allow.py`（新，12 项）。

### D-036 补丁 1 / 2：窗口豁免（口径 C）扩展

- **补丁 1（来源前缀）**：D-039 上线后带码图的视觉证据以「小程序码通过」开头，而原 `extract_wall_text`
  只认「校园墙白名单」→ **带码图之后的文字拿不到窗口豁免**。只读查库实证：**3 条**文字因此被真实撤回
  （2026-09-18 16:55:43 / 16:56:16 / 16:58:52，与前图相差 8 / 41 / 4 秒）。
  修复：`SOURCE_MARKS = (WALL_MARK, MINIPROGRAM_MARK)`，**两种前缀都必须出现在开头**（R09 语义不变）；
  「小程序码通过|」无「文案:」标记时取首个分隔符之后的整段作为图内文案（**仍要求非空**）。
- **补丁 2（负责人指令："2 分钟内发的文字和图片都不撤回，色情/暴力的文字不豁免"）**：
  豁免范围由"任意广告**文字**"扩展为**文字与图片**（`PAIR_EXEMPT_KINDS = {"text","image"}`）；
  不豁免判据由"非 ad 一律不豁免"改为**显式禁止集合** `PAIR_BLOCKED_CATEGORIES = {"porn","violence","flood"}`。
  - **诈骗（fraud）纳入窗口豁免**（降 `record_only` + 转人工记录，非静默放行）——依据负责人两次
    "严重类别 = 色情/暴力"口径推定，**待负责人书面确认**（见第六节）。
  - **刷屏（flood）刻意保留不豁免**（行为规则，豁免等于关掉刷屏防护）。
  - **结构性规则不受影响**：合并转发 / 群名片不在 `PAIR_EXEMPT_KINDS` 内，仍"一律撤回"；`video` 等未列类型不走豁免。
  - **实现收敛**：新增 `is_pairing_candidate(msg, decision)` 作为**唯一判定处**，`pipeline` 前置过滤
    （决定是否加载在途图片）与豁免函数共用，消除历史上"两处条件各写一遍"的漂移风险。
  - 保留不变：窗口 120 秒、按 `sent_at`、图必须严格先发（同秒不豁免）、来源图须视觉结构化确认、
    按 bot 账号/群/成员作用域隔离、无状态重算、未完成图片"不处罚也不豁免"。
- **文件**：`app/moderation/wall_pair.py`、`app/runtime/pipeline.py`、`tests/test_wall_pair.py`（+127 行，新增 7 项）。

### 其他

- `tests/test_r115_admin_migration.py`：探针不再硬编码 `alembic head`（改为读 `get_head_revision()`），
  避免每新增迁移都必然让 `alembic check` 失败。
- 文档：`DECISIONS.md`（+178）、`HANDOFF.md`（+94）、`PROGRESS.md`、`PROJECT_CONTEXT.md`、`MEMORY_INDEX.md`、
  `NEXT_TASKS.md`、`docs/group-rules.md`；新增 `docs/deploy-runbook-d037-d038.md`（RPO/RTO + 回滚）、
  `docs/evidence/2026-09-18-deploy-d037-d038.md`、`docs/review-2026-09-18-image-exemption-failure.md`。

## 三、待主审复核清单（本轮，按优先级）

| # | 优先级 | 复核对象 | 建议审什么 |
|---|---|---|---|
| 1 | **P0** | D-037 成员白名单全类别放行 | ①`POLICY_ALLOW_RULE_IDS` / `FULL_ALLOW_NO_UPGRADE_RULE_IDS` 是否真能堵住**所有**旁路（AI 主/二审、显式 `DR_`、媒体层、复核门）；②`load_allowlist_members` fail-closed 是否为"空集"；③文件全量同步语义（**停用而非删除**）与二次确认阈值是否足够防误清空；④`AdminAudit` 是否覆盖增删启停与导入；⑤唯一约束下的并发写入路径 |
| 2 | **P0** | D-039 小程序码放行 | ①`has_miniprogram_code` 严格布尔契约（非布尔拒收、仅视觉通道）；②例外集合只含 `porn/violence` + 本地硬证据——**是否存在"随便配张带码图"的绕过面**；③AI 缓存键是否含提示词指纹/policy 上下文（旧缓存失效）；④`t204-v15` 与 `.env` 是否同步、是否有"挂旧版本号"的分支 |
| 3 | **P0** | D-038 结构性撤回 + `R007` 撞号修复 | ①结构性规则编号**不得**落入内置 `R0\d\d` 段（已有回归）；②复核门硬证据集合是否只含内置三项 + `decision.py` 导出常量；③保护角色/成员白名单的**分支顺序**是否真的先于这两条；④`_contains_unreviewable_content` 改动是否影响其它降级路径（T-306 原意是否保留） |
| 4 | **P0** | D-036 补丁 2 窗口豁免扩展 | ①**诈骗纳入豁免**是否正确反映负责人口径（见第六节，需负责人确认）；②`flood` 保留不豁免是否与负责人"都不撤回"存在冲突；③`is_pairing_candidate` 与 `maybe_wall_text_pairing` 是否**同源**、pipeline 前置过滤是否可能漏查；④同秒 / 未完成图片的既有保护是否退化；⑤图片豁免是否引入"同一张放行图开道 + 图片刷屏"的新绕过面（与 `R005` 刷屏规则的交互） |
| 5 | **P1** | 提示词与版本 | `t204-v15`、`config/ai_prompt_rules.txt`（SHA256 见第一节）与 `.env` 三方一致；提示词中"小程序码/校园墙/严重类别"三条规则的边界是否互相矛盾 |
| 6 | **P1** | D-036 补丁 1 前缀扩展 | R09 前缀语义未被削弱（仍要求出现在开头）；空文案不构成来源；否定表述仍被拒 |
| 7 | **P1** | 文档一致性 | `DECISIONS.md`（D-036 两补丁、D-037~D-039）、`PROJECT_CONTEXT.md`、`docs/group-rules.md`、`HANDOFF.md` 与代码/线上事实是否一致；是否有"代码已改、文档未改"或反之 |
| 8 | **P2** | 迁移 | `c9a1f4d27e30` 与 `app/models.py` 是否一致、`downgrade` 是否可用（丢列/丢表语义是否可接受） |

## 四、本地与线上验证数据

| 项 | 结果 |
|---|---|
| 本地全量测试 | **1374 passed / 15 skipped / 0 failed**（15 项为环境依赖跳过） |
| 静态检查 | `ruff check` + `ruff format --check`（232 文件）、`mypy`（88 源文件）全部通过 |
| 配对相关 6 个测试文件 | **76 项全过** |
| D-037 回归 | `tests/test_r116_member_allowlist.py`（含端到端 `run_pipeline` 真实生效 + 影子零外呼） |
| D-039 回归 | `tests/test_r116_miniprogram_allow.py`（12 项，含"诈骗放行 + 色情/暴力仍不放行"参数化） |
| 线上只读核查 | 4000 条判定逐条解析：`has_miniprogram_code=true` → 全部放行 0 例外；被撤记录命中该标记 0 条；模型自述 8 条与字段一致 |
| 线上只读核查 | 小程序码图后 2 分钟内广告文字撤回：**3 条**（已由补丁 1 修复） |
| 部署 | `QQBotWeb`/`QQBotRuntime` Running；healthz `status=ok / mode=SAFE / onebot ready+connected`；三次提权重启 16:24（D-037/D-038/D-039 + v14）、17:06（v15 + 补丁 1）、17:18（补丁 2） |
| 生产动作快照 | 16 群中 **13 群启用真实动作**；今日 SUCCEEDED / SKIPPED / FAILED = 599 / 182 / 13（失败均为 QQ 侧 `recallMsg` 超时 code 1200） |

## 五、已知边界与未包含（透明说明）

1. **图片哈希黑白名单仍未持久化**：本地图片白/黑名单当前**完全失效**（`data/t002_media` 已被 15 天清理策略
   删除且哈希未落库，运行器每次启动告警 `image lists empty`；仓库内无副本）。需负责人重新提供原图后才能重建。
2. **`_CAMPUS_WALL_MARKERS` 是死代码**（`app/moderation/image_engine.py`，定义无调用）——本轮未处理。
3. **群名片识别尚无真实样本**：多信号宽容判定可能误判音乐/新闻/小程序卡片；需真实样本收窄。
4. **单视觉模型直接决定的比例偏高**：实测某日 237 条图片处罚中 201 条（85%）由单主视觉模型直接决定（无二审），
   与 `PROJECT_CONTEXT`"单模型只提供软证据"的原则不一致（现由 D-022 覆盖）；是否收紧**属负责人决策**。
5. **撤回通知（`group_recall`）仍未落库**：仅计数，未作为待标注事件。
6. **合并转发不做内容展开**：本轮口径为"一律撤回"，未调用 `get_forward_msg`（无内容审核、无嵌套处理）。
7. 本轮**未**做：容量重跑、真实样本收窄、遗留 N03（源消息时间持久化）等。

## 六、需负责人（非主审）确认的口径项

| # | 项 | 当前实现 | 待确认 |
|---|---|---|---|
| 1 | 窗口豁免内的**诈骗** | **纳入豁免**（降 `record_only` + 转人工） | 按负责人两次"严重类别=色情/暴力"口径推定，**需明确**；若要"诈骗照常撤"，改 `PAIR_BLOCKED_CATEGORIES` 一处 |
| 2 | 窗口豁免内的**刷屏** | **不豁免**（照常撤） | 负责人原话"都不撤回"，本轮刻意保留刷屏防护，**需明确**是否一并豁免 |
| 3 | 图片哈希白名单 | 未实现（无样本） | 需负责人提供"确定该放行"的原图 |
| 4 | 单模型自动处罚比例 | 允许（85% 图片处罚由单模型直接决定） | 是否要求二审/本地证据支持后才自动处罚 |
