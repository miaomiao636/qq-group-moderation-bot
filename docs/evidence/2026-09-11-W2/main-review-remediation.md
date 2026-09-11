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

## 尚未完成（如实声明）

- 新样本独立复测（S03 要求）：待冻结口径后取样。
- 同 SHA 远程 CI（主审建议）：待推送后创建 PR。
- 通知送达证据：待补充（当前为实施方自述接通）。
- 延迟端到端口径证据：需 onebot_inbox 独立测量（W3 期间采集）。
