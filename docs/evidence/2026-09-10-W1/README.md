# W1/T-303 影子接收窗口（2026-09-10 起）

状态：**进行中**（本文件记录启动基线；24h 汇总证据待窗口结束后补充）

## 启动基线

| 项 | 值 |
|---|---|
| 代码 SHA | `d77ea603cc3e3277b6d676fae5e333cead5da218`（main） |
| 窗口开始（本地 UTC+8） | 2026-09-10 约 18:06（服务 ready 时刻） |
| 窗口开始（UTC） | 2026-09-10T10:06:34Z（`last_connect_at`） |
| 计划结束 | 至少 2026-09-11 18:06（本地），即连续 ≥24h |
| 模式 | SAFE / `ACTION_MODE=SHADOW` |
| 动作开关 | `ONEBOT_ACTIONS_ENABLED=false`、`ONEBOT_ACTION_STAGE=recall_only` |
| DB 急停 | 已激活（`runtime_emergency_stop=true`） |
| 通知 | 全关（`NOTIFICATIONS_ENABLED=false`） |
| 服务 | `QQBotWeb` + `QQBotRuntime`（单实例，无 --reload/多 worker） |
| NapCat/QQ | NapCat 4.18.19 + QQ 9.9.31.49738 |

## 启动时就绪状态

- `/healthz`：`status=ok`、`mode=SAFE`、`onebot.state=ready`、`connected=true`、`login_state=online`、`storage_available=true`
- 队列积压 0；NapCat 反向 WS 已连接

## 待采集证据（窗口结束后填写）

- [ ] 逐类消息计数（text/image/gif/video/audio/file/share_card/mixed/unknown，含发送清单）
- [ ] 接收/判定去重计数、失联窗口、按群最后事件、队列积压
- [ ] 实际出站动作计数 = 0（`action_intents` 与 Adapter 调用双重证据）
- [ ] 故障演练：WebSocket 断开重连、QQ 退出/恢复、重复事件
- [ ] 正常静默群与消息断流的区分
- [ ] 缺口量化（不能仅凭 DB 内部自洽宣称零丢失）

## ⚠️ 模型切换与窗口重置（2026-09-10 晚）

因 AI 视觉模型延迟（mimo-v2.5 单次 25~43s）与精度口径问题，经评测后切换为：

| 项 | 旧 | 新 |
|---|---|---|
| AI 提供商 | `api.xiaomimimo.com` | `https://api.deepseek.com` |
| 文本/视觉模型 | `mimo-v2.5` | `deepseek-v4-flash-vision-exp` |
| prompt 版本 | `t204-v3` | `t204-v4` |
| 业务规则 | 硬编码 | 外部文件 `config/ai_prompt_rules.txt`（校园墙三特征 + 办证例外 + 平台黑话） |

**评测依据**：`docs/model-eval-2026-09-10.md`。延迟 text avg 1.4s / vision avg 2.1s（快 15~18 倍），
42 张人工标注图 precision 100% / recall 100%。

**W1 窗口从模型切换重启后重新计时 24h**（前一窗口数据作废，避免跨模型版本混算）。
服务重启后 `state=ready`、NapCat 自动重连；仍为 SHADOW 模式，`action_intents` 保持 0。

## 窗口内事件

| 时间(UTC) | 事件 | 说明 |
|---|---|---|
| 2026-09-10T10:28:28Z | 服务重启（stop→start） | 加载 `.env` 的 `AI_PROMPT_VERSION=t204-v2`。此前 `.env` 覆盖为 `t204-v1`，与代码 SYSTEM_PROMPT 变更不一致（属配置修正）。重启后 ~30s 内 `state=ready`，NapCat 自动重连（`connect_count=1`）。窗口连续性受此短暂中断影响，如实记录。 |

## 负责人决定（2026-09-10）

| 事项 | 决定 | 影响 |
|---|---|---|
| 群 G-006（`1090875633`） | **保持审核关闭**，不参与监管 | 该群消息不进审核；不计入 W1 类型覆盖 |
| 测试样本 | **依赖历史消息与自然产生的消息**，不主动构造样本 | W1 类型覆盖以自然流入为准；未出现的类型标注 NOT_TESTED |
| 故障演练 | **稍后安排**，先观察稳定接收 | WS 断开/QQ 退出演练推迟；窗口内先采稳定性数据 |

## 窗口内并行工作（不干扰接收）

- **W2 样本准备工具**已就位：`scripts/w2_sample_export.py` + `scripts/w2_sample_merge.py`；
  流程与延迟口径见 `docs/w2-sample-prep.md`。W1 窗口起点用 `2026-09-10 10:00:00`(UTC) 导出。
- **AI 延迟根因（已完成排查）**：`mimo-v2.5` 模型固有慢——text avg 25.4s / vision avg 32.2s，
  整体 p95 42.8s；非超时（60s 未触发）、非并发、非重试问题（错误率 0.8%）。
  ⚠️ 与 W2 延迟门槛（text ≤3s / image ≤15s）冲突，需与主审确认处理方式。
- **端到端延迟口径**：`onebot_inbox.updated_at - created_at`（DONE 行保留 180 天），
  仅 W0 live inbox 之后的事件可用；此前记录只能回退到 AI 调用延迟。

## 关联

- W0 基线证据：`docs/evidence/2026-09-10-W0/w0-deployment.md`
- 历史 T-303 原件核验：`docs/evidence/2026-09-09-t303-history/README.md`
- W2 样本准备与延迟口径：`docs/w2-sample-prep.md`
