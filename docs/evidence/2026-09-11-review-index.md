# 主审核验索引（2026-09-11）

分支 windows-deploy-2026-09-10，SHA 6db4f35。原件在本机受控路径，摘要已脱敏。

| 阶段 | 文档 | 结论 |
|---|---|---|
| W0 部署 | docs/evidence/2026-09-10-W0/w0-deployment.md | 迁移ok 门禁全绿 SHADOW+急停 |
| P1-13 历史 | docs/evidence/2026-09-09-t303-history/README.md | 原件核验通过 |
| W1-lite | docs/evidence/2026-09-10-W1/README.md | 1h窗口PASS NOT_TESTED如实声明 |
| W2 最终 | docs/evidence/2026-09-11-W2/w2_runF_final.json | precision/recall 100% thresholds全过 |
| W2 复核 | docs/evidence/2026-09-11-W2/w2-failure-review.md | 26个失败逐张目验=标签过期 模型0真实错误 |
| 模型评测 | docs/model-eval-2026-09-10.md | deepseek延迟2.3s 办证例外口径确认 |

## 当前配置（生产）

- 模型: deepseek-flash（主）+ qwen3.8-flash（灰区复核）
- 提示词: t204-v7 + config/ai_prompt_rules.txt（严格口径: 推广外部产品=ad）
- 模式: SAFE / SHADOW / 动作关 / DB急停激活 / action_intents=0
- 通知: QQ群+邮件双通道（均已真实验收）

## NOT_TESTED / 风险声明

1. 长时稳定性(>1h)与故障演练(WS断开/QQ退出): 未测, 待隔离群授权后 W3 补齐
2. 语音/文件/转发/引用消息类型: 未自然出现, 未覆盖
3. W2 真值经两轮人工复核修正(26条), 每条有逐张目验依据, 原始标签备份 data/w2_labels_all.pre-*.jsonl
4. W2 延迟为replay压力口径; 生产e2e按onebot_inbox口径, W1-lite内正常
5. deepseek-v4-flash-vision-exp 已被官方退役, 已迁移 deepseek-flash(同一后端)

## 排期依据

docs/windows-delivery-checklist.md: W2已过 -> W3/W4(隔离群授权+演练) -> W5(目标群试运行, 须先签认隐私告知/规则例外/人工接管责任)

