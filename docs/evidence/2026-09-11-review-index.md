# 主审核验索引（2026-09-11）

分支 windows-deploy-2026-09-10。原件在本机受控路径，摘要已脱敏。
**本索引按证据分项声明 PASS / FAIL / NOT_TESTED，不将未测写成通过。**

## 证据入口

| 阶段 | 文档 | 结论 |
|---|---|---|
| W0 部署 | docs/evidence/2026-09-10-W0/w0-deployment.md | 迁移 ok；门禁全绿；SHADOW+急停 |
| P1-13 历史 | docs/evidence/2026-09-09-t303-history/README.md | 原件核验通过 |
| W1-lite | docs/evidence/2026-09-10-W1/README.md | 1h 简化窗口（负责人决定）；PASS 项与 NOT_TESTED 见该文档 |
| W2 结果 | docs/evidence/2026-09-11-W2/w2_runF_final.json | 见下方分项 |
| W2 复核 | docs/evidence/2026-09-11-W2/w2-failure-review.md | 26 条失败逐张目验；模型 0 真实错误 |
| 标签修订 | docs/evidence/2026-09-11-W2/label-revisions.md | 26 条修订的匿名映射、理由、批准与摘要 |
| 模型评测 | docs/model-eval-2026-09-10.md | 延迟/精度评测与办证例外口径 |

## W2 分项结论（口径: deepseek-flash@t204-v7，160 条开发回归集）

| 项 | 结论 | 说明 |
|---|---|---|
| 整体 precision | **PASS**（1.00） | 标签复核后 |
| 整体 recall | **PASS**（1.00） | 同上 |
| ad 类别召回（≥90%） | **PASS** | ad 类是主要交付类别 |
| **fraud 类别召回** | **NOT_TESTED**（null） | 本批无 fraud 正例，不可声明通过 |
| porn 类别 | 覆盖不足 | 1 正例 0 负例 |
| 视频延迟 | **NOT_TESTED** | 无视频样本；文本/图片延迟为 **replay 压力口径**，非端到端 |
| 端到端延迟门槛 | **NOT_TESTED** | 需 onebot_inbox 口径独立测量 |
| release_decision | REQUIRES_HUMAN_REVIEW | 机器报告原文 |
| 留出集独立性 | **未满足** | 同一 160 条参与过调规则/调提示词/复核标签，不能单独证明长期 100%；正式放行须用未参与调整的新样本、按近重复模板隔离后复测 |

## 当前配置（生产）

- 模型: deepseek-flash（主）+ qwen3.8-flash（灰区复核）
- 提示词: t204-v7 + config/ai_prompt_rules.txt（严格口径: 推广外部产品=ad）
- 模式: SAFE / SHADOW / 动作关 / DB急停激活 / action_intents=0

## 通知通道（如实表述）

- QQ 群 + 邮件双通道：**实施方自述接通并经发送侧验证**（SMTP status=SENT、QQ 群测试消息已发送）；
- **主审待核验**：真实送达回执、真人确认、备份升级路径、整机失联/恢复证据未提供。
- 注：本项与 W1 启动表"通知全关"属不同时间状态（通知于 W1 窗口后开启）。

## NOT_TESTED / 风险声明

1. 长时稳定性（>1h）与故障演练（WS 断开/QQ 退出）: 未测，待隔离群授权后 W3 补齐。
2. 语音/文件/转发/引用消息类型: 未自然出现，未覆盖；不得写成已支持。
3. W2 真值经两轮人工复核修正（26 条），每条有逐张目验依据；原始标签备份
   `data/w2_labels_all.pre-*.jsonl`，修订映射见 label-revisions.md。
4. 零动作证据: action_intents=0 不单独证明真实外部处罚调用为 0；仍需 Adapter/传输侧计数。
5. deepseek-v4-flash-vision-exp 已被官方退役，已迁移 deepseek-flash（同一后端）。
6. 主审 5739b5d 复审提出 S01–S10；整改状态见 docs/evidence/2026-09-11-W2/main-review-remediation.md。

## 放行路径（不因"报告全绿"自动开启）

W3/W4 需隔离群明确授权 → W5 前须签认隐私告知/规则例外/人工接管责任；
W2 正式复测须新样本 + 同 SHA 远程 CI。真实动作保持关闭直至负责人明确授权。
