# 真实动作恢复证据：2026-09-15（W3+W5 合并进行）

授权：负责人明确指示"外部心跳不考虑，其他两个（W3 单群实弹 + W5 全量）全部进行"，即恢复 5 个已授权群的真实撤回（`recall`）。本文件为部署侧生效证据，不替代主审独立复验。

## 1. 操作

1. `.env`：`ONEBOT_ACTIONS_ENABLED=false → true`（2026-09-15 19:37 本地；`ONEBOT_ACTION_STAGE` 保持 `recall_only`——只撤回，禁言/警告在编排层仍被跳过；`EMERGENCY_STOP=false` 保持不变）。
2. 服务重启（NSSM，UAC 提权 19:38）：`QQBotRuntime` + `QQBotWeb`——healthz `status=ok / mode=SAFE / onebot=ready / connected=true / queue_backlog=0`；web 监听进程 PID `32852 → 40760`，`processed_total` 计数器复位重新统计。

## 2. 生效证据（重启后）

**首条真实撤回执行成功**（`action_intents`）：

| 字段 | 值 |
| --- | --- |
| id | `1013` |
| action / status | `recall` / **`SUCCEEDED`** |
| 群 / 目标成员 / 消息 | `17598122` / `1174247768` / `2098062009` |
| result | `{"action":"recall","ok":true,"status_code":0,"err_code":null,"attempts":1}`（一次成功） |
| actor / 时间 | `system` / 2026-09-15 11:39:58Z（19:39:58 本地），created→updated 0.34s |

对照（同一开关、重启前后）：

- 重启前最后一条同场景：intent `1010`（群 `470794920`，11:33:57Z）→ `SKIPPED`，原因「OneBot真实动作开关未开启（ONEBOT_ACTIONS_ENABLED），仅记录不执行」。
- 重启后未授权群仍被拦截：intent `1012`（群 `983434859`，11:38:45Z）→ `SKIPPED`，原因「群动作已禁用（管理员设置）」——授权范围未扩大。

即：**新开关已加载（进程重启即读 `.env`）→ 授权群端到端真实撤回成功 → 非授权群边界不变**。

## 3. 范围与回退

- 动作范围：仅 `recall`（stage=`recall_only`）；仅 5 个已授权群（`group_action_owners` + `provider_group_settings.action_enabled=1`：17598122 / 470794920 / 6638171 / 869142826 / 894723164）。
- 保护机制（均在本次重启版本中有效）：急停三级（`EMERGENCY_STOP` 配置 / 运行时急停 / DB 急停——不重启即停）；保护角色拦截；`violation_high` 前置；人工纠正在场检查；群级开关。
- 回退：`.env` 改回 `false` + 重启两个服务；或立即拉急停。
- 观察项：NapCat 撤回接口历史超时 11 次（09-14，`NodeIKernelMsgService/recall Timeout`）——失败会记录 `FAILED` + 转人工，不静默；本轮恢复后继续观察。

## 4. 边界

- 本操作经负责人授权（"其他两个可以全部进行"）；未扩大群范围、未提升 stage、未改处罚判定逻辑。
- 未做生产删除、未重写 Git 历史；合并与文档同步见对应 PR。
