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
| 整体 precision | **PASS**（1.00） | 标签复核后（开发回归集口径） |
| 整体 recall | **PASS**（1.00） | 同上 |
| ad 类别召回（≥90%） | **不可判（仅供参考）** | 旧 debug 的 truth_category 全为空，该数字来自旧预测分类口径；标签修订只闭合总体真假标签，未补齐独立人工类别。**待真值类别重跑** |
| **fraud 类别召回** | **NOT_TESTED**（null） | 本批无 fraud 正例，不可声明通过 |
| porn 类别 | 覆盖不足 | 1 正例 0 负例 |
| 视频延迟 | **NOT_TESTED** | 无视频样本；文本/图片延迟为 **replay 压力口径**，非端到端 |
| 端到端延迟门槛 | **部分采集，text 未达** | 生产 SHADOW `onebot_inbox` 口径（1819 事件，2026-09-10~11）：overall p95 479ms；**text p95 17.4s 超出 3s 门槛**（AI 调用抖动/排队）；image p95 102ms；详见 `e2e_latency_2026-09-10_to_11.json`。text 超门槛原因与处置需专项分析 |
| release_decision | REQUIRES_HUMAN_REVIEW | 机器报告原文 |
| 留出集独立性 | **未满足** | 同一 160 条参与过调规则/调提示词/复核标签，不能单独证明长期 100%；正式放行须用未参与调整的新样本、按近重复模板隔离后复测 |

## 当前配置（生产）

- 模型: deepseek-flash（主）+ qwen3.8-flash（灰区复核）
- 提示词: t204-v7 + config/ai_prompt_rules.txt（严格口径: 推广外部产品=ad）
- 模式: SAFE / SHADOW / 动作关 / DB急停激活 / action_intents=0

## 通知通道（如实表述）

- QQ 群通道：**8 条真实运营通知全部 SENT**（2026-09-11 12:36–14:19，`notification_deliveries` 实测），内容为单位化摘要不含群内容；
  **真人确认已闭环**（负责人 2026-09-11 答复"看到了"）。
- 邮件通道：SMTP 端到端验证 `status=SENT` + **负责人收件确认**（测试邮件截图）。
- fallback 升级路径（QQ 失败→邮件）：机制存在且已配置，**NOT_TESTED**（本窗口零失败未触发）。
- 备份收件人：未配置。整机失联/恢复：未做（W3）。
- 详见 `docs/evidence/2026-09-11-W2/notification-acceptance.md`。

## NOT_TESTED / 风险声明

1. 长时稳定性（>1h）与故障演练（WS 断开/QQ 退出）: 未测，待隔离群授权后 W3 补齐。
2. 语音/文件/转发/引用消息类型: 未自然出现，未覆盖；不得写成已支持。
3. W2 真值经两轮人工复核修正（26 条），每条有逐张目验依据；原始标签备份
   `data/w2_labels_all.pre-*.jsonl`，修订映射见 label-revisions.md。
4. 零动作证据: action_intents=0 不单独证明真实外部处罚调用为 0；仍需 Adapter/传输侧计数。
5. deepseek-v4-flash-vision-exp 已被官方退役，已迁移 deepseek-flash（同一后端）。
6. 主审 5739b5d 复审提出 S01–S10；整改状态见 docs/evidence/2026-09-11-W2/main-review-remediation.md。

## 放行路径（不因"报告全绿"自动开启）

1. **先补正式 W2 证据**：未参与调整的新样本 + 端到端延迟（onebot_inbox 口径）——
   二者均可在 **SHADOW 阶段**采集，不必等真实动作。
2. W2 通过 **且** 负责人明确授权后，才进入 **W3**（隔离群演练，含 WS 断开/QQ 退出恢复）。
3. 独立动作通道演练若作为例外提前开展，须明确例外范围，**不能悄悄替代完整 W3 门禁**。
4. W5 前须签认隐私告知/规则例外/人工接管责任；真实动作保持关闭直至负责人明确授权。
