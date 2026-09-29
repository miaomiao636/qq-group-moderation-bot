# r132 主审报告独立复核（提交方自查，2026-09-18）

> 对象：主审包 `qqbot-review-pr45-84a8b47`（REVIEW.md / TEST_RESULTS.md / 6 个探针文件）。
> 受审版本：PR #45 head `84a8b47`。方法：**在本机 Windows 复跑主审探针**（`-c pyproject.toml -p conftest`，
> 临时库 + SHADOW + 禁用真实 AI/动作），并逐条对照源码与文档。
> 结论：**主审结论成立，本轮不通过**。以下逐条记录复核结果与证据；未做修复（待负责人授权后按批整改）。

## 一、复跑主审探针的结果（本机 Windows / Python 3.12 / 仓库当前 = 受审 SHA）

命令与主审 README 一致：

```powershell
$env:PYTHONPATH=".;tests"
uv run python -m pytest -c pyproject.toml -p conftest "<ReviewPack>\probes" -q -o addopts= --tb=line
```

**结果：12 failed / 30 passed**（6.06 秒），失败清单与主审报告逐项对应：

| 失败探针 | 对应发现 | 复核 |
|---|---|---|
| `test_window_evidence.py::...[porn-0.99-0.95]`、`[violence-0.99-0.95]` | F01 多图时色情/暴力被窗口豁免遮蔽 | ✅ 复现 |
| `test_window_evidence.py::test_window_must_preserve_real_flood_hit` | F08 刷屏被 ad 主类别遮蔽（基线已有） | ✅ 复现 |
| `test_r132_qr_unresolved.py::...[unresolved-True]`、`[timeout-True]` | F02 QR 图替另一未决附件放行 | ✅ 复现 |
| `test_r132_structure_probe.py::test_descriptive_text_does_not_become_structural_group_card[news-mentioned-group-card/music-ordinary-phrase]` | F03 普通卡片文案被当群卡硬证据 | ✅ 复现 |
| `test_r132_structure_probe.py::test_group_card_structural_rule_survives_mixed_text[with-text]` | F04 文字+群卡绕过结构性撤回 | ✅ 复现 |
| `test_r132_structure_probe.py::test_member_policy_allow_not_downgraded_by_unavailable_content[unknown-segment/missing-image]` | F07 D-037"不转人工"仍有旁路 | ✅ 复现 |
| `test_member_import_review.py::test_import_requires_a_server_bound_preview`、`test_stale_preview_must_not_reenable_a_revoked_member` | F05 导入确认未绑定预览 | ✅ 复现 |
| `test_r132_qr_contract.py`（12 项）、`test_same_second_pending.py`（4 项） | 控制组 | ✅ 全过 |

**主审报告的一处笔误（不影响结论）**：REVIEW.md §1 写"12 failed / 26 passed"，
README.md 与其实际输出、以及本机复跑均为 **12 failed / 30 passed**。

## 二、逐条复核（源码证据）

| 编号 | 复核结论 | 源码证据（本仓库当前代码） |
|---|---|---|
| **F01** 窗口豁免只看主类别 | **属实（P1）** | `wall_pair.py::is_pairing_candidate` 仅看 `decision.category`；而 `ai.py::merge_ai_evidence` 对多候选取 `max(confidence)` 作为 `category`，其余命中仅留在 `rule_hits`。故 ad 0.99 + porn 0.95 同条消息 → 主类别 ad → 豁免 → `record_only` 且清空动作。**与本轮自己文档写的"色情/暴力不豁免"自相矛盾** |
| **F02** QR 图替另一附件放行 | **属实（P1）** | `ai.py::_miniprogram_qr_allow` 用 `any(...has_miniprogram_code)` 做**消息级**判断，且在未决/降级汇总**之前**返回；`needs_review=True` 或 `degraded_reason` 的另一附件不构成阻断 → 整条消息 `allow`。违反 R-108"审核未完成保守转人工" |
| **F03** 普通卡片文案被判群卡 | **属实（P1）** | `parser.py::_is_group_card` 末行 `return "群名片" in desc or "推荐群" in desc`——对 `desc`/`prompt` 做**子串**匹配。新闻卡"新版 QQ 群名片设置使用教程"、音乐卡"推荐群友听一首好歌"均命中 → `R_GROUP_CARD` → `violation_high` + recall/mute/warn 建议 |
| **F04** 文字+群卡绕过结构性撤回 | **属实（P1）** | `rules.py:514` `share_card = msg.kind == "share_card"`，`:535` `group_card = share_card and ...`。text+card → `kind="mixed"` → 两条分支都不进（`R006` 同样被绕过）→ `allow` |
| **F05** 导入确认未绑定预览 | **属实（P1）** | `routes.py::allowlist_members_import`：确认分支**重新计算** `plan_member_import` 并直接 `apply_member_import`；预览摘要仅经隐藏 `textarea` 回传，无签名/绑定/一次性约束；`confirmed=1` 可脱离预览直接写库 |
| **F06** 已提交版本无法按手册回滚 | **属实（P1）** | `docs/deploy-runbook-d037-d038.md` B1 用 `git stash push -u` 撤销代码（对已提交内容无效，`get_head_revision()` 仍 `c9a1f4d27e30`）；B2 的 `git checkout -- <paths>` 无目标 ref → 只回到当前版本 |
| **F07** D-037"不转人工"旁路 | **属实（P2，非误撤）** | `pipeline.py` 的 `evidence_vetoes`（未知段/缺图/同群多 provider 等）无条件把决策降为 `record_only`，未区分"名单身份"与其他安全异常；`FULL_ALLOW_NO_UPGRADE_RULE_IDS` 未覆盖这些通用 veto |
| **F08** 刷屏被 ad 遮蔽（基线已有） | **属实（P2，非本轮新增）** | `rules.py:507-508` `confidence = max(...)` + `category = category or "flood"` → ad 在前则保持 `ad`，`R005` 只留在 `rule_hits`；本轮的窗口豁免因此也能免掉刷屏。**另有文档/代码阈值不一致**：`FLOOD_MAX_MESSAGES = 3`（60 秒内 ≥3 条）vs `DECISIONS.md`「1 分钟 >5 条」 |
| **F09** 提示词与版本标识 | **属实（P2）** | ①`config/ai_prompt_rules.txt:7-13`"无论任何外观都不豁免 fraud" 与 `:27`"含码诈骗放行"冲突，未写优先级；②`app/config.py:141` 默认 `t204-v6`、`.env.example:63` `t204-v4`（生产 `.env` 为 v15，属新部署审计标签漂移）；③`decision.py:34-37` 注释仍写"两个例外：①严重类别（诈骗/色情/暴力）"，代码已不含 fraud。④CRLF/LF 造成的 SHA256 差异——主审已明确不据此误报，认可 |
| **F10** 状态与统计缺可核验证据 | **属实（P2）** | ①`PROJECT_CONTEXT.md:7` 仍写"**尚未部署、未实机验收**"，`NEXT_TASKS.md` 送审链仍指 `2d01974`/`c66820a`、D-039 仍写 `t204-v14`、"例外=严重类别 B-2"；②把 `c9a1f4d27e30`（迁移 revision）称作"代码 head"；③4000 条只读核查的**诊断脚本已用完删除**，无 manifest/导出可重算；④日统计未固定 UTC 半开窗、未绑定部署 SHA；⑤`code 1200` 只能证明调用超时，不能推断 QQ 侧未执行 |

## 三、主审未提出的两项补充（提交方自查发现）

1. **PR #45 处于 `DIRTY / CONFLICTING`**（`headRefOid=84a8b47`）：`main` 已前进，重新送审前需先合并 `main` 并复跑门禁。
2. **主审"已通过"清单中的 `c9a1f4d27e30` 可逆性**：`downgrade` 会 `DROP` 整张 `allowlist_members` 表 →
   **名单数据丢失**（不是无损恢复）。回滚手册需显式写明并给出导出/回填步骤（与 F06 同一整改）。

## 四、整改计划（建议顺序；待负责人授权后执行）

**第一批（P1，阻断项）**

1. **F01+F08 合并修复**：窗口豁免与 `pair_blocked` 判据改为**检查全部仍有效的类别证据**
   （`rule_hits` 中 `porn/violence/flood` 任一存在即不豁免），而非只看 `decision.category`；
   同时修 `category = category or "flood"` 的遮蔽（严重类别/刷屏不被广告主类别遮蔽）。
   配套：把主审两组反例转为正式回归；保留正常广告窗口正例。
2. **F02**：`_miniprogram_qr_allow` 增加"**全部被审附件均已定论**"前置条件
   （任一 `needs_review`/`degraded_reason`/缺二审 → 不授予放行，落回既有 record_only 路径）；
   按 `review_group` 绑定到具体附件，禁止替其它附件完成审核。
3. **F03**：删除 `desc/prompt` 子串判定，改为只认**结构化字段**（`meta.group` 结构 / `app` 精确值 /
   `view == "group"`）；拿不到可靠结构时**不**作为硬证据撤回（宁少撤不误撤）。
4. **F04**：结构性规则改为按**消息段/卡片结构**判断（`msg.share_card` 或任一段为卡片），
   不再依赖顶层 `kind`；覆盖 text+卡、image+卡、多卡；保护角色与 D-037 顺序不变。
5. **F05**：预览确认绑定为**一次性凭据**（绑定 operator + 会话 + 规范化文件摘要 + 计划指纹），
   执行时校验计划未变（事务内条件更新），状态改变必须重新预览；审计记录"批准的差异"与"实际结果"。
6. **F06**：重写"已提交/已部署版本"的回滚章节：明确目标 Git SHA、对应 schema、名单导出/回填、
   库备份、不确定动作禁止重放；在隔离临时库完成往返演练后再上生产。

**第二批（P2 与交接）**

7. F07：按 D-037 契约区分"名单身份确认"与"其他安全异常"（只对名单身份抑制通用 veto，不得扩大范围）+ 2 条全管线回归。
8. F09：提示词写清例外优先级；同步 `app/config.py` 默认与 `.env.example` 到当前版本；修 `decision.py` 注释。
9. F10：状态文档给"截至何时"的当前态；证据格式分离 **Git SHA / schema head / prompt version+digest**；
   补可重算导出（脚本 + manifest + hash，匿名最小字段）；统计固定 UTC 半开窗并绑定部署 SHA。
10. 合并 `main`、解决 PR 冲突、复跑门禁，按主审模板重新送审（含三 job CI 证据）。

## 五、需负责人拍板的口径项（主审 §5）

| # | 项 | 现状 | 待负责人 |
|---|---|---|---|
| 1 | 窗口内**诈骗**是否豁免 | 已按推定纳入（`record_only`） | 主审建议**明确前不扩大**；需负责人书面确认或撤回 |
| 2 | 窗口内**刷屏**是否豁免 | 代码意图"不豁免"，但 F08 使其可能被遮蔽 | 建议继续不豁免，先修 F08 |
| 3 | 图片哈希白名单 | 未实现（无样本） | 需负责人提供原图并确认用途 |
| 4 | 单模型直接决定 85% 图片处罚 | 允许 | 是否按条件启用二审（成本变化由负责人选择） |
| 5 | 名单大幅停用阈值 | 5 条**且** 30% | 是否改为"任一触发" |
| 6 | D-038 动作等级 | 建议 recall/mute/warn，执行层 recall_only | "一律撤回"不自动授权提高禁言/警告等级 |
