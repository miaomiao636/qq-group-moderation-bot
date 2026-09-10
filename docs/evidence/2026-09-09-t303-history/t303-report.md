# T-303 Windows 隔离群影子验证 —— 24 小时数据汇总（脱敏副本）

窗口：2026-09-08 19:50 ~ 2026-09-09 19:50（本地 UTC+8）／UTC 11:50~11:50
环境：Windows 专机，NapCat 4.18.19 + QQ 9.9.31（OneBot 反向 WS），SAFE/SHADOW
原始数据（本机受控）：`data/t303_summary.json`；断线证据（本机受控）：`data/drill-log-2026-09-09.md`

> 说明：本文件是历史原件 `data/t303-report.md` 的脱敏副本。真实群号已映射为匿名代号（见 README 映射规则），统计数字与结论未改动。原件 SHA-256 见 `ORIGINALS-SHA256.txt`。

## 1. 消息量与类型覆盖（937 条影子判定）

| 类型 | 数量 | 说明 |
|---|---|---|
| text | 473 | 文字（allow 187 / record_only 264 / violation 22） |
| image | 293 | 图片 |
| mixed | 96 | 图文混合 |
| unknown | 53 | 未知段（含卡片/降级），全部保留元数据转人工，未误判为正常 |
| share_card | 20 | 分享卡片 |
| video | 2 | 视频 |

## 2. 判定分布

- record_only 722（77.1%）、allow 193（20.6%）、**violation_high 22（2.3%）**
- 22 条违规全部仅记录；`action_intents` 表全量为 **0** ——影子模式外部动作调用为 0 ✅

## 3. 群覆盖（6 个 OneBot 群 + 官方通道残留）

G-001(314) / G-002(204) / G-003(163) / G-004(93) / G-005(83) / G-006(76)，
共 933 条 via provider=onebot；另有 4 条 qq_official 残留（接入 NapCat 前的官方通道测试数据）。

## 4. 事件处理完整性

- processed_events 941：PROCESSED 937、FAILED(lease_expired) 4（架构审查时清理的僵尸租约，非丢失）
- **消息丢失 0、重复处理 0**（processed_events 主键去重 + 演练 2 重启前后 DB 逐表一致）

## 5. AI 调用（936 次，全部可追溯）

| 来源 | 次数 | 成功率 | 备注 |
|---|---|---|---|
| text | 330 | 100% | 文字审核 |
| vision | 157 | 100% | 多模态审核 |
| cache | 439 | 100% | 缓存命中 |
| degraded | 10 | — | AI 失败降级为记录+人工，服务未中断 |

延迟：p50 29.5s / p95 41.5s / max 213.9s（mimo-v2.5 模型固有延迟；缓存命中同样计入端到端耗时）。

## 6. 断线与恢复（详见 drill-log）

- WS 闪断 1 次：4 秒自动重连
- 后端崩溃重启 1 次：瞬时重连，数据零丢失零重复
- QQ 进程全灭 1 次：服务如实告警 offline/degraded，快速登录免扫码约 2 分钟恢复

## 7. 结论（供主审核对 NEXT_TASKS T-303 完成标准）

- ✅ 连续影子运行 ≥24 小时
- ✅ 主要消息类型稳定转换（text/image/mixed/video/share_card/unknown）
- ✅ 重复事件不重复处理
- ✅ NapCat/QQ/OneBot 失效能被检测并告警（degraded/offline 状态如实呈现）
- ✅ 外部管理动作调用数 = 0
- ⚠️ 待主审确认项：3 条 qq_official 残留判定（external_group_id 为空）为历史数据，建议清理或忽略
