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

## 需要负责人配合

1. **测试消息**：在隔离/测试群按类型发送样本（文字、图片、GIF、表情、视频、语音、文件、转发卡片、引用），以便覆盖消息类型。
2. **群审核开关**：当前仅有群 G-006 存在显式设置且 `moderation_enabled=0`（审核关闭）。若需监管该群，请在管理后台开启审核；其他群默认开启。
3. **故障演练时间**：需安排 WebSocket 断开/QQ 退出等演练窗口（可参照 `data/drill-log-2026-09-09.md` 的历史做法）。

## 关联

- W0 基线证据：`docs/evidence/2026-09-10-W0/w0-deployment.md`
- 历史 T-303 原件核验：`docs/evidence/2026-09-09-t303-history/README.md`
