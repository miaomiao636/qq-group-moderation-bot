# W0 更新后基线证据（2026-09-10）

阶段：W0（代码更新后基线）
结论：**PASS**
执行人：Windows 部署 Agent（本机）
开始/结束（本地时间 UTC+8）：2026-09-10 17:57 ~ 18:07

## 版本与代码

| 项 | 值 |
|---|---|
| 更新前代码 SHA | `0cbafa3f6ce5a2264434c8962040961d3d459561`（feature/t307-onebot-actions，PR#5 整改前） |
| 更新后代码 SHA | `d77ea603cc3e3277b6d676fae5e333cead5da218`（main，含 PR#5 整改 + R-105 + R-106） |
| 更新方式 | GitHub tarball 通道（git 协议在本机被重置，api/codeload 可达）；robocopy 覆盖，保留 .git/.venv/data/.env |
| Python | 3.12.10 |
| uv | 0.12.9 |
| uv.lock SHA256(前16) | 198CFE9754B9B16D |
| Windows | Windows 10 专业版 10.0.19045（64 位） |
| Windows 补丁 | KB5126256（2026-09-09）、KB5071982、KB5071959 |
| QQ | 9.9.31.49738 |
| NapCat | D:\QQ\napcat.mjs（3092561 字节，2026-08-14），4.18.19 注入式 |

## 安全开关（脱敏，无凭据）

重启后 `.env` 显式锁定：
```
APP_ENV=local
ACTION_MODE=SHADOW
ONEBOT_WS_ENABLED=true        # 入站（非处罚开关）
ONEBOT_ACTIONS_ENABLED=false  # 处罚关闭
ONEBOT_ACTION_STAGE=recall_only
NOTIFICATIONS_ENABLED=false   # 通知全关，本轮不接通
EMERGENCY_STOP=false          # 启动级；另有 DB 急停
WEB_PORT=8001
```
DB 急停：`runtime_emergency_stop` 已激活（`active: True`，revision 已更新），经 app 正式函数写入并留审计。

## 备份

| 项 | 值 |
|---|---|
| 备份目录 | `data/backups/w0-20260910-175738/` |
| 内容 | moderation.db + moderation.db-wal + moderation.db-shm + .env.bak |
| integrity_check | `ok` |
| 备份时 alembic | `e1a4b8c2d3f5` |
| 备份时数据 | shadow_decisions 2004 行 / processed_events 2008 行 / action_intents 0 行 |
| 回滚方法 | 停服务 → 用上述文件集还原 data/ → 恢复 .env.bak → 以 SHADOW 启动 |

## 迁移

| 项 | 值 |
|---|---|
| 迁移前 | `e1a4b8c2d3f5` |
| 迁移链 | `e1a4b8c2d3f5 → b1d3f5a7c909`（R-105 provider 设置/动作所有权/人工变更计划）→ `c2e4f6a8b010`（R-105 持久 OneBot inbox）→ `d3f5a7b9c111`（R-106 通知三表） |
| 迁移后 | `d3f5a7b9c111 (head)` |
| 表数量 | 23 → 31 |
| 数据保留 | shadow_decisions 2004 行、processed_events 2008 行（与备份一致） |
| 动作位 | action_intents 0 行；group_settings 中 G-001 action_enabled=0（安全关闭） |
| 新增表 | onebot_inbox、notification_notices、notification_deliveries 等 |

## 质量门禁（本机实测）

| 检查 | 结果 |
|---|---|
| `uv sync --locked` | exit 0 |
| `uv run ruff check app tests alembic` | All checks passed |
| `uv run ruff format --check app tests alembic` | 172 files already formatted |
| `uv run mypy app` | Success: no issues found in 83 source files |
| `uv run pytest` | **757 passed, 1 warning in 137.00s** |

## 服务重启与就绪

- 单实例重启：`QQBotWeb` + `QQBotRuntime`（未启用官方通道运行器、无 --reload、无多 worker）
- 两服务状态：Running
- `/healthz`：`status=ok`、`mode=SAFE`（SHADOW）
- OneBot：`state=ready`、`connected=true`、`login_state=online`、`storage_available=true`
- NapCat：服务启动后自动重连，心跳新鲜（<10s）

## 待办与提醒

- **群 G-001 的 `moderation_enabled=0`**：该群审核处于关闭状态，W1 影子接收不会审核该群消息。若需监管该群，请负责人在管理后台开启审核（本 Agent 不擅自更改）。
- 通知（R-106）保持全关，本轮零外呼，待负责人批准与凭据到位后另做。
- W1/T-303 需至少连续 24h 实机影子接收；P1-13 历史证据原件已确认在位（见下一阶段）。
