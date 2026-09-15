# 真实动作恢复证据：2026-09-15（W3+W5 合并进行）

授权：负责人明确指示"外部心跳不考虑，其他两个（W3 单群实弹 + W5 全量）全部进行"，即恢复真实撤回（`recall`）——首批 5 个授权群，随即（同日 19:53）负责人经后台再扩容 6 个，合计 11 个。本文件为部署侧生效证据，不替代主审独立复验。

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

### 2.1 第二批扩容生效（19:53 负责人后台操作）

19:53:17~19:53:51，负责人在管理后台为 6 个群开启真实动作（每群 plan_create → plan_approve → plan_execute 三步，`admin_audits` #710–730）。生效证据：

| intent | 群 | 时间 | 结果 |
| --- | --- | --- | --- |
| `#1016` | `544140282`（日结兼职交流群） | 12:02:34Z | `SUCCEEDED`（该群首条真实撤回） |
| `#1017` | `869142826`（高校表白墙） | 12:03:36Z | `SUCCEEDED` |

重启后至本记录时：**3 条真实撤回全部成功（#1013/#1016/#1017）、0 失败**；未授权群继续被拦截。

### 2.2 DB 急停现场演练（2026-09-15 深夜，负责人实际操作）

主审 R-112 N02 要求"操作人实际验证 DB 急停"。完整演练链（全部审计可查）：

| 步骤 | 时间 | 证据 |
| --- | --- | --- |
| 激活急停（后台"立即停止全部外部动作"） | 13:29:32Z | `admin_audits` #742 `emergency_stop`（active: true）；`system_settings.runtime_emergency_stop='true'` |
| **实际阻断**（在途违规撤回尝试被拦） | 13:35:09Z | `action_intents` #1041：群 `983434859`（**已授权群**，正常会真实执行）→ `SKIPPED`，原因"急停开关开启，禁止外部动作"——**无外发** |
| 恢复（"预览解除急停"确认流程） | 13:35:35Z | `admin_audits` #745 `emergency_resume`（active: false） |
| 状态确认 | 即时 | `system_settings.runtime_emergency_stop='false'`；`emergency_stop_state.active=False` |

结论：**DB 急停跨进程即时生效（无需重启）、实际阻断真实动作且留审计、恢复走完整确认流程**——主审 N02 现场验证要求完成。（13:26 首次快速演练激活后 19 秒即恢复、窗口内无动作尝试，未捕获拦截样本，不计入本证据。）

## 3. 范围与回退

- 动作范围：仅 `recall`（stage=`recall_only`）；**11 个授权群**——首批 5 个（17598122 / 470794920 / 6638171 / 869142826 / 894723164）+ **2026-09-15 19:53 负责人经后台扩容 6 个**（330380007 / 476796763 / 544140282 / 67602330 / 868995385 / 983434859，每个群均走 plan_create→approve→execute 三步流程，`admin_audits` #710–730 可查）。
- 保护机制（均在本次重启版本中有效）：急停入口——**后台/API 的 DB 急停跨进程、不重启即生效**（编排外发前重读 DB；`EMERGENCY_STOP` 环境变量与运行时急停属进程内/需重启加载——R-112 N02 更正）；保护角色拦截；`violation_high` 前置；人工纠正在场检查；群级开关。
- 回退：`.env` 改回 `false` + 重启两个服务；或立即拉急停。
- 观察项：NapCat 撤回接口历史超时 11 次（09-14，`NodeIKernelMsgService/recall Timeout`）——失败会记录 `FAILED` + 转人工，不静默；本轮恢复后继续观察。

## 4. 边界

- 本操作经负责人授权（"其他两个可以全部进行"）；未扩大群范围、未提升 stage、未改处罚判定逻辑。
- 未做生产删除、未重写 Git 历史；合并与文档同步见对应 PR。
