# QQ 群多模态智能管理机器人

面向 QQ 群的多模态内容审核与自动管理：24×7 识别广告、诈骗及自定义违规内容（文字 / 图片 /
GIF / 表情 / 视频 / 语音 / 文件 / 卡片），自动执行高置信消息的撤回与分级禁言，整理两次违规
证据并交由人工决定是否踢人；全部判定与动作可审计、可急停、可回退。

> **交付说明**：本项目面向单一接收方交付（文件包）。使用**自己的 QQ 账号、自己的群、自己的
> 密钥**从零部署，即为独立安装。**新部署 = 新现场验收**：代码行为一致，生效证据由各部署方
> 自行采集（见 §7）。
> 交付包中的历史文档用 `<BOT_QQ>` / `<OLD_BOT_QQ>` / `<OWNER_EMAIL>` 占位符代替了原部署的
> 真实账号与邮箱；`.env`、`data/`、日志等运行数据不在包内。

## 能力概览

- **多模态审核**：文本 / 图片 / GIF / 表情 / 视频 / 语音 / 文件 / 卡片
- **分级处置**：记录 → 撤回 → 1 小时禁言 + 警告 → 24 小时禁言（高置信自动执行）；
  两次违规合并为案件，由人工决定是否踢出
- **策略保护**：全局白名单（仅免广告）、办证/学历内容放行、群主/管理员卡片放行
  （均可在代码策略与后台配置，详见 `DECISIONS.md` 中的 D-031/D-032/D-033）
- **管理后台**：群管理、案件处理、影子记录、白名单、动态规则、通知中心、审计
- **安全底线**：急停开关（只记录不动手）、按群动作开关、动作结果未知不盲目重放、全链路审计
- **主通道**：NapCat + OneBot 11（反向 WebSocket）；可选 QQ 官方机器人通道

## 架构

```
QQ 群消息
   │
   ▼
QQ 客户端 + NapCat（同机运行，反向 WebSocket 客户端）
   │  ws://127.0.0.1:<WEB_PORT>/onebot/ws   （Authorization: Bearer <令牌>）
   ▼
审核服务（本仓库，Python 3.12）
   ├─ 消息解析 → 规则引擎（本地/动态）→ AI 复核（可选）→ 判定与分级
   ├─ 动作编排（撤回 / 禁言 / 警告：按群授权 + 急停 + 结果未知保护）
   ├─ 案件与证据、违规累计、审计
   ├─ 通知（QQ 群 / 邮件，可选，默认全关）
   └─ 管理后台（HTTP，默认仅本机）
   │
   ▼
SQLite（data/moderation.db，WAL）+ 媒体文件（data/）
```

| 组件 | 说明 | 是否必须 |
| --- | --- | --- |
| Web 服务（`python -m app`） | 管理后台 + OneBot 反向 WS 接收 + 通知工作器 | 必须 |
| 运行器（`python -m app.runtime`） | 常驻影子运行器（后台任务） | 必须 |
| NapCat | QQ 协议端；需交互式登录（扫码/快速登录），**不能**注册为 Windows 服务 | 必须 |
| NSSM | 把上面两个 Python 进程注册为 Windows 服务（开机自启） | 推荐 |
| SMTP / AI Key | 通知与 AI 复核 | 可选 |

## 1. 环境要求

- Windows 10/11（建议**专用机** 24×7 在线：禁止睡眠/休眠；BIOS 开启"恢复供电自动开机"）
- Python 3.12+、[uv](https://docs.astral.sh/uv/)、Git
- NapCat（自带 QQ 客户端）
- （可选）[NSSM](https://nssm.cc/)：放入如 `C:\nssm\nssm.exe`

## 2. 从零部署

### 2.1 获取代码并安装依赖

```powershell
cd <项目目录>
uv sync --all-groups
```

### 2.2 配置 `.env`

```powershell
Copy-Item .env.example .env
```

必填 / 常用项（完整注释见 `.env.example`）：

| 键 | 说明 |
| --- | --- |
| `WEB_HOST` / `WEB_PORT` | 后台监听地址与端口（默认 `127.0.0.1:8000`；本文示例用 `8001`） |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 后台登录；`APP_ENV=prod` 时必须强口令 |
| `ONEBOT_WS_ENABLED=true` | 启用 OneBot 反向 WS 接收 |
| `ONEBOT_ACCESS_TOKEN` | 强随机令牌（NapCat 与系统共用，建议 ≥32 字符） |
| `ONEBOT_SELF_ID` | **你的机器人 QQ 号** |
| `ONEBOT_ACTIONS_ENABLED` | 真实动作总开关；**首次部署保持 `false`**（先影子观察） |
| `ONEBOT_ACTION_STAGE` | 先 `recall_only`（只撤回）；确认稳定后按需 `full` |
| `ACTION_MODE` | `SHADOW`（默认，只记录）/ `OFFICIAL`（允许真实动作，另需 prod、口令、非急停） |
| `AI_*` / `NOTIFICATION_*` | 可选；通知详见 `docs/proactive-notifications.md` |

> ⚠️ `.env` 只在本机保存：**永不提交仓库、永不随交付包分发**。

### 2.3 数据库迁移

```powershell
uv run alembic upgrade head
```

全新安装 = 空库；迁移会自动建表。

### 2.4 前台试运行与验证

```powershell
uv run python -m app           # 终端 A：Web + OneBot WS + 通知工作器
uv run python -m app.runtime   # 终端 B：常驻运行器
```

- 健康检查：`http://127.0.0.1:<WEB_PORT>/healthz` → `"status":"ok"`
- 管理后台：`http://127.0.0.1:<WEB_PORT>/admin` → 用 `ADMIN_*` 登录

### 2.5 接入 NapCat（反向 WebSocket）

1. 安装并登录 NapCat（扫码或快速登录；**不能**注册为 Windows 服务——需要交互式会话）
2. NapCat WebUI →「**网络配置**」→ 添加「**WebSocket 客户端**」：

   | 字段 | 值 |
   | --- | --- |
   | URL | `ws://127.0.0.1:<WEB_PORT>/onebot/ws` |
   | Token | 与 `ONEBOT_ACCESS_TOKEN` 完全一致 |
   | 消息格式 | `Array` |
   | 启用 | ✓ |

3. 保存后 NapCat 自动连接（列表显示"已连接"）。验证：
   - `/onebot/status`（带 `Authorization: Bearer <令牌>`）→ `self_id` = 你的机器人 QQ、`login_state=online`
   - `/healthz` → `onebot.state = ready`
4. 常见问题：连不上 → 检查 `WEB_HOST/PORT`、令牌一致性、防火墙；本地明文 `ws://` 如遇证书
   校验拦截可关闭"SSL 证书验证"；令牌仅经 `Authorization` 头传递，**不要**拼接进 URL。

### 2.6 服务化（开机自启）

```powershell
# 管理员 PowerShell：
powershell -ExecutionPolicy Bypass -File scripts\install-services-nssm.ps1 `
  -ProjectDir "<项目路径>" -NssmPath "C:\nssm\nssm.exe"
```

脚本注册两个服务（基于 NSSM）：

- `QQBotWeb` = `python -m app`；`QQBotRuntime` = `python -m app.runtime`
- 自动启动；崩溃 5 秒后自动重启；日志轮转到 `data\service_*.log`（10MB 轮转）

> ⚠️ **中文路径坑**：PowerShell 5.1 会把无 BOM 的 UTF-8 脚本按 GBK 解析——含中文的安装脚本
> 必须以 **UTF-8 带 BOM** 保存。本仓库的脚本为纯 ASCII，避免此问题；自行修改时请留意。

NapCat 自启：把 `scripts\napcat-autostart-template.bat` 复制到「启动」文件夹
（`Win+R` → `shell:startup`），按注释填好路径与机器人 QQ——登录 Windows 后自动拉起
（带单实例保护，避免重复启动）。

电源设置：控制面板 → 电源选项 → "使设备进入睡眠" = **从不**；BIOS 开启来电自启。

## 3. 首次验收检查（新部署 = 新现场验收）

| # | 检查 | 通过标准 |
| --- | --- | --- |
| 1 | 服务 | `sc query QQBotWeb` / `QQBotRuntime` = RUNNING；`/healthz` = ok |
| 2 | NapCat | `/onebot/status` = online；`/healthz` napcat = ready |
| 3 | 后台 | `/admin` 登录正常 |
| 4 | 影子链路 | 在机器人所在任意群发一条消息 → 后台「影子记录」出现该消息判定（只记录，不动手） |
| 5 | 群授权 | 在「群管理」添加要管理的群；**动作开关保持关闭**，先观察 |
| 6 | 动作权限 | 机器人须为群**管理员/群主**方能撤回；开通动作前逐群确认 |
| 7 | （可选）通知 | 按 `docs/proactive-notifications.md` 验收 QQ 群 / 邮件通道 |

## 4. 默认运行口径（部署后请知悉）

- **判定与记录**：机器人所在群收到的消息默认**全部判定入库**（无记录群默认审核开启，
  可在群设置中调整）——这是影子观察的基础。
- **真实动作**：仅发生在**显式授权**（群管理里动作开关打开）的群；未授权群只记录不动手。
- **首次上手建议**：先全量影子观察 1–2 天 → 逐步给群开动作（先 `recall_only`）→ 稳定后再考虑
  禁言/警告档位。

## 5. 日常运营（管理后台）

- **群管理**：按群开关审核 / 动作
- **案件**：两次违规合并 → 人工决定（踢出 / 误报 / 保留观察）
- **白名单**：仅免广告；诈骗/色情/暴力证据照常处理
- **动态规则**：后台发布（全局或按群）
- **急停**：一键切换"只记录不动手"（跨进程立即生效，无需重启）
- **备份**：整备 `data/` 目录（含 `moderation.db` 与媒体）；恢复 = 放回并重启服务
- **升级**：`git pull` → `uv sync --all-groups` → `alembic upgrade head` → 重启两个服务

## 6. 质量门禁（开发 / 验收）

```powershell
uv run pytest                       # 全量回归
uv run ruff check app tests alembic
uv run ruff format --check app tests alembic
uv run mypy app
```

## 7. 文档导航

| 主题 | 文档 |
| --- | --- |
| Windows 运行要求与恢复模型 | `docs/windows-operations.md` |
| 交付与实测清单 | `docs/windows-delivery-checklist.md` |
| 部署配置对照表（复刻同款效果） | `docs/deploy-config-reference.md` |
| 更换机器人 QQ 账号 | `docs/switch-qq-account.md`（含实操记录） |
| 主动通知（QQ 群 / 邮件） | `docs/proactive-notifications.md` |
| 决策与审计 | `DECISIONS.md` |
| 当前进度 / 后续任务 | `PROGRESS.md` / `NEXT_TASKS.md` |

## 8. 许可

MIT License，见 [`LICENSE`](LICENSE)。
