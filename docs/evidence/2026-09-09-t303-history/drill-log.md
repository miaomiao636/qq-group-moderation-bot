# 断线演练记录（T-303 验收证据，脱敏副本）

日期：2026-09-09　环境：Windows 专机，NapCat 4.18.19 + QQ 9.9.31，影子模式（SAFE/SHADOW）

> 说明：本文件是历史原件 `data/drill-log-2026-09-09.md` 的脱敏副本。机器人 QQ 号已匿名化为 `BOT-QQ-01`，结果未改动。原件 SHA-256 见 `ORIGINALS-SHA256.txt`。

## 演练 1：WebSocket 断开（人工操作 NapCat WebUI）

| 项目 | 结果 |
|---|---|
| 操作 | WebUI 停用反向 WS → 约 4.5 分钟后重新启用 |
| 断开检测 | `last_disconnect_at = 19:20:34`，disconnect_count=1 |
| 自动重连 | `last_connect_at = 19:25:07`，connect_count=2，无需人工干预 |
| 恢复后状态 | `state=ready`，login=online，心跳正常 |
| 结论 | **通过** |

## 演练 2：机器人后端崩溃重启（kill 全部 python 进程树）

| 项目 | 结果 |
|---|---|
| 操作 | taskkill 终止 `python -m app`（Web/OneBot WS）与 `python -m app.runtime` 两棵进程树，含遗留 pytest 进程 |
| 崩溃时间 | 约 19:36（8001 端口释放，确认全下线） |
| 重启 | 后台重启两个服务，uvicorn 启动正常 |
| NapCat 重连 | **瞬时重连**（11:37:26Z = 服务监听后立即连入），无需重启 NapCat |
| ready 恢复 | 重连后约 40 秒内首个心跳到达，degraded → ready（心跳机制按设计工作） |
| 数据完整性 | DB 前后对比：processed_events 932→932，shadow_decisions 928→928，其余各表全部一致——**无重复处理、无数据丢失** |
| 动作安全 | action_intents 前后均为 0；runner 日志确认 `shadow mode (record only)` |
| 结论 | **通过** |

## 演练 3：QQ 进程终止与恢复（2026-09-09 19:45–19:49）

| 项目 | 结果 |
|---|---|
| 意外闪断 | 19:45:07 用户在任务管理器结束 QQ 子进程 → WS 断开，**4 秒后自动重连**（connect_count 1→2），闪断自愈 ✅ |
| 正式演练 | 19:46:29 taskkill 终止 QQ 主进程树（含 NapCat 注入的 QQ.exe 全部进程） |
| 故障检测 | 服务端立即呈现 `connected:false, login_state:offline, state:degraded`——**未假装正常**，符合"告警转人工"要求 ✅ |
| 恢复 | `D:\QQ\launcher-user.bat BOT-QQ-01` 快速登录（无需扫码），QQ 重启后 NapCat 立即重连（11:48:11，connect_count=3），新消息继续处理（processed_total 5） |
| ready 恢复 | 19:49:11 心跳到达，degraded → ready ✅ |
| 经验教训 | ① 必须用 `launcher-user.bat`（设置 NAPCAT_* 环境变量）启动，裸调 NapCatWinBootMain.exe 无法加载；② 快速登录凭据有效，掉线恢复无需人工扫码；③ 恢复全程约 2 分钟，其中人工参与仅"运行启动脚本"一步 |
| 结论 | **通过** |

## 证据文件（本机受控）

- `data/drill2_baseline.json`：崩溃前 DB 快照
- `data/drill2_web.log` / `data/drill2_web.err.log`：Web 服务重启日志（含 WS accepted）
- `data/drill2_runtime.log`：runtime 重启日志（shadow mode 确认）
- `/healthz` 快照：见上方表格数据（connect_count/disconnect_count/心跳）

## 备注

- runtime 启动时有一条既有 WARN：`hash failed t002_01_evt13_member.pdf`（PDF 非受支持图片格式被正确拒绝），属媒体类型过滤正常行为，非本次演练引入。
- 演练 2 后服务改为后台进程运行（日志落 data/ 目录），原两个终端窗口已不再承载服务。
