# 主审 5739b5d 复审整改记录（2026-09-11）

对象：`C:/Users/81596/Desktop/qqbot-review-5739b5d.md`（S01–S10）。
执行：Windows Agent；批准：负责人（2026-09-11）。整改代码在 SHADOW/动作关下进行，零外部风险。

## 整改状态

| 编号 | 问题 | 整改 | 验证 |
|---|---|---|---|
| S01 | 跨模态矛盾直接升罚 | `app/moderation/ai.py`：新增 `_confident_normal`/`_cross_modal_veto`；任一模态给出"确定性正常"或"要求人工"时，另一模态不得单独升罚（双方向） | 新增 5 个 merge 单元回归 + 4 个经完整 `AIReviewService` 的编排回归（文字广告/图文正常→无动作；文字要人工/视觉违规→无动作；对称方向；纯文字广告不误伤） |
| S02 | 分类召回按预测类别虚高 | `w2_sample_merge.py`：缺/非法 truth_category **排除样本**（不再回退 other）；`w2_replay.py` 保持逐样本版本字段 | 新增 `tests/test_w2_toolchain.py`：漏判广告留 ad 分母（recall 50% 复现场景） |
| S03 | 索引宣称"全过"证据不足 | 重写 `review-index.md`：分项 PASS/FAIL/NOT_TESTED；fraud 召回/视频延迟/端到端延迟=NOT_TESTED；声明同批样本非独立留出集；新增 `label-revisions.md` 归档 26 条修订 | 文档修订 |
| S04 | 盲标页 `C` 未定义 | `w2_label_page.py`：初始化 `C`（含旧缓存容错 `load()`） | 回归测试断言定义先于 render |
| S05 | 回放输出与合并器契约不兼容 | `w2_sample_merge.py`：缺 `model_revision/rule_revision` 时明确报错（不再 KeyError）；replay debug 已逐样本写入版本字段 | 工具链测试 `test_s05_legacy_debug_without_revision_is_rejected` |
| S06 | rules_digest 定位到父目录 → missing | `w2_replay.py`：改用运行时同款 `_resolve_rules_path`；配置了规则却 missing 时**中止评测**；manifest 增加 `worktree_dirty` | 工具链测试 + 复跑核对 |
| S07 | AI 子链耗时冒充端到端 | 回放输出 `latency_ms=null` + `latency_source="none"`（AI 耗时只留 debug）；`evaluation.py` 新增 `latency_source` 契约：无 inbox 证据 → p95/门槛=未测 | 工具链测试 2 项（none→None；inbox→保留数值；非法组合拒绝） |
| S08 | 降级失败不计入失败率 | `w2_replay.py`：degraded/异常/缓存/真实调用分别统计，manifest 输出 `degraded/unusable/fail_rate`；`w2_sample_merge.py` 排除降级样本 | 工具链测试 `test_s08_degraded_samples_excluded_from_eval` |
| S09 | 关 AI 仍被缺规则阻断 | `ai_wiring.py`：禁用分支先于规则加载返回；启用时仍 fail-closed | 新增 2 项测试（禁用+缺规则可用；启用+缺规则拒绝） |
| S10 | 群名读错响应层级 | `onebot_wiring.py`：校验 status/retcode 后读 `data.group_name`；失败允许有限退避重试（600s） | 新增 3 项测试（成功保存/非ok可重试/失败释放并退避） |

## 当时尚未完成（历史状态，2026-09-12 更新见下）

- 新样本独立复测（S03 要求）：待冻结口径后取样。
- 同 SHA 远程 CI（主审建议）：待推送后创建 PR。
- 通知送达证据：待补充（当前为实施方自述接通）。
- 延迟端到端口径证据：需 onebot_inbox 独立测量（W3 期间采集）。

## 合并与远程 CI（2026-09-11 晚）

- 与 origin/main (d77ea60) 合并，解决 9 个文件冲突（保留本分支 v6 规则/群备注统一/批量导入/跨模态 S01 逻辑；
  main 侧自动合并保留），并修复合并引入的 review_vision_moderator 重复定义。
- 合并后全量 pytest 通过（exit 0；13 项 data 守卫在服务运行中按设计跳过，CI 环境全跑）。
- 推送后 GitHub Actions 触发并 **success**（run 34608272484，6m17s）。PR #7 状态 CLEAN / MERGEABLE。

---

## 主审复验（aa04f7f）A01–A04 整改

| 编号 | 问题 | 整改 |
|---|---|---|
| A01 | 回放默认输出可产生虚高分类召回 | replay 默认输出标记 `category_source=model_predicted`；正式聚合器明确拒绝非 manual_truth 输入（必须先经 merge）|
| A02 | 降级/异常样本被删除致分母缩水 | merge 不再删除：降级/异常保留在端到端召回分母（真实结果=未自动识别）并标记 `unavailable`；报告输出 unavailable 数量与 `measurement_complete` 完整性门槛（>5% 判不完整）|
| A03 | 延迟来源仍被猜测/覆盖未声明 | merge 不再推断：无显式 inbox 声明一律未测；报告输出按类型延迟有效样本与缺失数、覆盖率；门槛需覆盖≥95% 才可判通过（否则未测）|
| A04 | 有效二审被旧 needs_review 标记否决 | 视觉侧 veto 延后到复核对解析完成后计算：已获有效二审确认的 primary 不再被原始 needs_review 否决；未消解的 gray/brief仍保持安全转人工（3 项正向/反向回归）|

验证：全量 pytest 通过（新增/更新 A01–A04 回归 9 项）；ruff/format/mypy 全绿。
CI：合入后推送取得新 SHA 远程检查（见下方最新 run 记录）。
文档校正（主审第四节）：索引 ad 类别改"不可判（待真值重跑）"；旧复核报告加历史说明并指向新索引；本记录 CI run 更新；放行顺序明确"端到端延迟可在 SHADOW 采集、W3 不可被例外替代"。

## 2026-09-12 主审复验补充（R-107）

- 受审 `ddf1f73214015d8df6eff71606b6f3ade4909ea3` 的远程 CI [34619565035](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/34619565035) 已核验三项 success；PR #7 当前 OPEN / CLEAN / MERGEABLE，不是已合入 main。
- A01–A03 主路径及 A04/S01 编排修复有效；另补默认 replay 元数据重新 merge 时丢失 unavailable 标记的边界修复，不再把测量完整性误报为通过。
- 通知文档已补负责人确认；这关闭“是否收到”这一项，但不等于 ack 接手、QQ 失败后升级或整机失联演练已完成。
- 延迟原始 JSON 保留；旧脚本按外部消息号关联有跨账号/通道串关联风险，已改完整事件键并增加失败/未完成/未关联计数。原窗口需 Windows 用新版只读脚本复算，不得在 Mac 补造历史证据。
- 新独立样本仍待复测；端到端可在 SHADOW 采集。纯文字路径不调用视觉二审，17.4s 的成因不能仅凭聚合报告归为“二审叠加”。不调整既有 3s 门槛，不以异步入队时间替代完整审核时间。
