# r132 二轮整改报告（2026-09-18 深夜）

> 对象：主审二轮复验包 `qqbot-review-pr45-a354d17`（受审 `a354d17`，结论"整改有效但未达关闭标准"）。
> 独立复核：本机（Windows）复跑二轮新增探针——**能跑的全部对上**（`test_f05_plan_integrity.py` 6 项、
> `test_qr_review_pair_unresolved.py` 2 项 = 8 failed）；其余 8 项被主审包的"禁止网络连接"守卫在
> Windows 上误拦（ProactorEventLoop 的 socketpair 会触发本机回环 connect），**已按代码逐条确认成立**。

## 0. 结果

| 指标 | 二轮整改前 | 整改后 |
| --- | --- | --- |
| 二轮新增探针（本机） | 8 failed / 15 passed（另 21 error 为守卫误拦） | **全部通过** |
| 一轮 42 项探针 | 42 passed | 42 passed（未回退） |
| 全量测试 | 1418 passed / 15 skipped 部分失败 | **1461 passed / 15 skipped / 0 failed** |
| 静态门禁 | — | `ruff check` + `ruff format --check`（241 文件）、`mypy`（88 源文件）通过 |
| 迁移 | 未新增（head 仍 `c9a1f4d27e30`） | 同左 |

二轮探针已**原样入库**：`tests/test_r132_f05_plan_integrity.py`、`tests/test_r132_qr_review_pair.py`、
`tests/test_r132_window_card_adjacent.py`（仅加文件级 lint 豁免头；`test_r132_window_card_adjacent.py`
另有一处**非断言**的 Windows 适配：守卫仍禁止一切非本机连接，仅放行 loopback）。

## 1. P1 逐项

### F02-R 二审不一致/置信不足仍被另一张带码图放行
- **修改**：`app/moderation/ai.py`
  - 抽出 `_secondary_pair_is_valid()`（二审是否构成有效结论：视觉、未降级、非同一模型、不需人工、
    类别一致、首轮≥低阈值、二审≥高阈值）——**主路径的复核对校验改为调用它**（单一判据来源）；
  - 新增 `_attachment_reviews_unresolved()`：任一首轮要求复核却没有**恰好一个**有效二审，或存在孤儿二审 → 未消解；
  - `_miniprogram_qr_allow()` 在授予放行前先判"全部附件已定论"（含上面的复核对状态）。
- **探针**：`test_qr_review_pair_unresolved.py` 2 例 → 通过；`normal` 场景 + 带码仍放行（控制不变）。

### F04-R 图片+群卡被窗口取消结构撤回
- **修改**：`app/moderation/wall_pair.py`
  - 新增 `STRUCTURAL_RULE_IDS = {FORWARD_RECORD_RECALL_RULE_ID, GROUP_CARD_RECALL_RULE_ID}`（按 `decision.py`
    导出常量，**不猜 `R0xx` 段号**）；`is_pairing_candidate()` 命中结构性规则即**不进窗口候选**。
- **回归**：`tests/test_wall_pair.py::test_structural_card_hit_is_not_window_exempted`；
  主审探针 `test_image_group_card_always_retains_structural_recall`（两种段顺序 × 窗口内外）。

### F03-R app 名含 group/qun 直接成为群卡硬证据
- **修改**：`app/adapters/onebot/parser.py::_is_group_card()` **删除 app 子串判定**，只认
  ① `meta.group` 且含 `groupCode/groupName/groupUin/memberNum` 之一；② `app == "com.tencent.qun.share" 且 view == "group"`
  的明确组合。拿不到可靠结构 → 不作硬证据。
- **探针**：`com.example.groupbuy` / `com.example.quniversity` 两例不再命中（`com.tencent.news` 控制不变）。

### N01 新增的诈骗来源限制会遮蔽合格来源、并越过在途保护（上一轮引入的退化）
- **修改**：`app/moderation/wall_pair.py`
  - 来源扫描**不再 break**：收集**全部**合格来源（`eligible_sources`），诈骗豁免按"**存在**合格小程序码来源"判定 → **与 SQL 返回/插入顺序无关**；
  - **前缀 + 结构化布尔同时成立**才算合格带码来源（`_confirmed_source()` 第三返回值取同一视觉结果的
    `has_miniprogram_code is True`）；
  - **在途图片保护独立计算**（`pending_messages` 扫描不再被"已有合格来源"跳过）→ 待审图不会被旁路。
- **回归**：`test_fraud_source_prefix_alone_is_not_enough`、`test_fraud_source_scan_ignores_row_order`；
  主审探针 `test_source_row_order_does_not_hide_eligible_qr_source`、
  `test_fraud_source_prefix_requires_matching_qr_flag`、`test_same_second_pending_image_protects_fraud_with_prior_campus_source` 全部通过。

### F05-R 批准与写名单之间的竞态（含 ABA）
- **修改**：`app/moderation/allowlist.py` + `app/web/routes.py`
  - `MemberSyncPlan` 增加 `row_versions`（被改动行的预览时 `updated_at`），并纳入批准指纹；
  - `set_member_enabled()` **显式推进 `updated_at`**（即便 enabled 值未变）——让"对已停用行的再次明确停用"
    也成为可检测的撤权；
  - `apply_member_import(..., commit=False)` 改为**条件写入**（`UPDATE ... WHERE id=? AND updated_at=?`），
    0 行命中即抛 `ConcurrentMemberChangeError` → 整体回滚并要求重新预览；
  - 确认流程重排为：**锁内校验归属/状态/指纹（新会话 + `BEGIN IMMEDIATE`）→ 批准 + 一次性认领 →
    锁内条件写入 + 导入审计 + 计划终态（同一事务）**。归属校验在批准**之前**，不再破坏其它会话的计划。
- **探针**：`test_f05_plan_integrity.py` 6 项全部通过（含跨会话不消费他人计划、撤销/删除后不被覆盖、
  备注变更需重新预览、终态与名单原子、审计失败整体回滚）。

### F06-R 回滚手册：停服等待 / 一致性备份 / 导出源
- **修改**：`docs/deploy-runbook-d037-d038.md` §3 B 重写为四步：
  ①**等待两个服务 `Stopped`**（含 60 秒超时中止，明确 `STOP_PENDING ≠ STOPPED`）；
  ②用**项目一致性备份入口**留证（数据库为 WAL，禁止只 copy 主库文件）；
  ③从**降级前的当前库**导出名单并核对启用数（**修正**：`allowlist_members` 是本次迁移新建表，旧备份里没有）；
  ④降级/切版本**逐条检查退出码**，失败即中止。并保留"先隔离演练、不得业务时段降库"。

## 2. P2

| 项 | 处理 |
| --- | --- |
| 备注未绑定 | `row_versions` 纳入指纹 + 条件写入（`test_note_changed_since_preview_requires_new_preview` 通过） |
| 计划终态与名单分两次提交 | 现已**同一事务**（`test_applied_marker_and_members_are_atomic` 通过） |
| 跨会话破坏原计划 | 归属/参数校验提前到批准**之前**（`test_cross_session_rejection_must_not_consume_other_session_plan` 通过） |
| 允许来源卡片 + 普通文字误转人工 | `_is_share_source_allowed()` 改为按**卡片结构**判断（与 D-032/D-038 同源） |
| "仅带码来源"只信前缀 | 现要求**前缀 + 结构化布尔**同时成立（见 N01） |
| F09/F10 收尾 | 仓库内提示词版本三处为 `t204-v16`；固定 UTC 窗口统计**仍待补**（未宣称完成） |

## 3. 仍未完成（如实登记）

1. **新 SHA 的三 job CI**：需在本次提交后重新产出（Ubuntu / Windows / clean runtime-deps）。
2. **固定 UTC 半开窗口统计导出**（绑定账号/群集合/部署 SHA），`code 1200` 仅作"调用超时"证据。
3. `PROGRESS.md` / `HANDOFF.md` 中"急停开启/解除待办"的旧文字需在下次状态更新时清理。
4. 负责人决策项（未越权改动）：图片哈希白名单缺样本、单模型直接决定 85% 图片处罚、名单大幅停用阈值、D-038 动作等级。
5. **Windows 侧窗口类探针**依赖主审包的 socket 守卫；本机已做 loopback 兼容适配并全过。
   ⚠️ **更正（主审 r132 复验指出）**：此前写"Ubuntu CI 将按原样跑主审版本"**不成立**——入库探针的
   loopback 兼容守卫**在 Ubuntu 上也生效**，仓库 workflow 只执行 `uv run pytest`、没有"运行外部原包
   或在 CI 里恢复原守卫"的步骤。正确表述：**断言相同、使用同一 loopback 兼容守卫**（原守卫的
   deny-all 语义仅在其他平台保留，Windows 另放行字面 loopback 以兼容 ProactorEventLoop；所有非本机
   连接仍被拒绝）。若坚持"按原样跑"，需另加独立 job/日志作为证据。

## 4. 口径边界（主审已向负责人发问，负责人本次已明确）

**窗口内诈骗豁免仅限"带小程序二维码"的来源图**（负责人 2026-09-18 晚确认）：来源图必须同时满足
证据前缀「小程序码通过」**与**结构化字段 `has_miniprogram_code=true`；校园墙来源图之后的诈骗照常处理。
窗口内的在途（未审完）图片保护**与来源是否存在无关**，始终生效。
