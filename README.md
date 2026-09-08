# QQ 群多模态智能管理机器人

24×7 识别 QQ 群中的垃圾广告、诈骗及自定义违规内容，支持文字、图片、GIF、表情、视频、语音、文件和卡片；自动执行高置信消息的撤回和分级禁言，整理两次违规证据并交由人工决定是否踢人。

> 当前阶段（2026-09-08）：个人认证官方机器人无法开启“添加到任意群聊”，目标大群不显示该机器人。决策D-019已将生产大群主通道改为NapCat/OneBot，官方机器人降为可选/测试通道。R-104已完成并通过真实Ubuntu、Windows和干净运行时CI；当前代码尚未实现NapCat入站及撤回/禁言/警告Adapter，下一项只可开始T-305，不得用于目标大群真实处罚。

## 架构概览

- **NapCatQQ + OneBot 11**：目标大群的计划主消息和撤回/禁言/警告通道；当前尚未实现。
- **QQ 官方机器人**：已实现的可选/测试Adapter，只能用于它实际可进入的群。
- **审核服务**：规则、行为、多模态识别、独立复核、违规累计、案件、证据、审批和报告。
- **人工 QQ 客户端 / NapCat**：互斥的踢人执行出口，踢人必须人工批准；首版可仅使用人工QQ客户端踢人。
- 详见 `PROJECT_CONTEXT.md`、`DECISIONS.md` 与 [`AI辅助审核、动态规则与反馈学习设计`](docs/superpowers/specs/2026-09-06-ai-rule-learning-design.md)。

## 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)（依赖与虚拟环境管理）

## 安装

```bash
# 1. 克隆仓库后进入项目根目录
cd qq-group-moderation-bot

# 2. 创建虚拟环境并安装依赖（含开发依赖）
uv sync --all-groups

# 3. 复制环境模板并填写真实值（真实凭据绝不提交仓库）
cp .env.example .env
```

## 运行

```bash
# 启动服务器（读取 .env 中的 WEB_HOST 与 WEB_PORT，默认 127.0.0.1:8000）
uv run python -m app
```

> 启动前必须先执行数据库迁移（见下节），否则应用会因数据库未初始化而拒绝启动。

健康检查：`GET http://127.0.0.1:8000/healthz`（端口以 `.env` 中 `WEB_PORT` 为准）

当前常驻运行器是**QQ官方通道影子运行器**，只能用于机器人实际可进入的隔离群：

```bash
uv run python -m app.runtime
```

该入口需要本地配置QQ官方凭据。默认 `ACTION_MODE=SHADOW`，只记录审核建议和模拟动作，不执行撤回、禁言或警告。它不是目标大群的NapCat运行命令；NapCat运行入口、配置名和安装步骤将由T-306/T-404实现后写入本文件，在那之前不得虚构命令。

开发时如需热重载，可显式指定端口：
```bash
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## 数据库迁移

```bash
# 生成迁移脚本（模型变更后）
uv run alembic revision --autogenerate -m "描述"

# 应用迁移
uv run alembic upgrade head
```

## 质量门禁

```bash
# 单元测试
uv run pytest

# 代码格式化（检查）
uv run ruff format --check app tests alembic

# 代码格式化（自动修复）
uv run ruff format app tests alembic

# 静态检查
uv run ruff check app tests alembic

# 类型检查
uv run mypy app
```

CI（`.github/workflows/ci.yml`）会在每次 push/PR 时，在 Linux 与 Windows 上自动运行以上全部检查。

> 当前基线提醒（2026-09-08）：R-104已完成，提交 `2d8f405` 的CI运行 `34186194703` 在Ubuntu、Windows与干净运行时任务全部成功。下一项只可开始 `NEXT_TASKS.md` 的T-305。CI仍有部分Action使用已弃用Node.js 20运行时的非阻塞警告，后续应安排维护升级。

## 目录结构

### 当前实际存在的目录

```text
app/            # 应用包
  actions/      # 官方撤回/禁言/警告动作编排（默认SHADOW）
  config.py     # 应用配置（环境变量，含校验）
  db.py         # 数据库引擎与会话（SQLite WAL）
  models.py     # 系统、事件去重与动作审计模型
  main.py       # FastAPI 应用入口
  __main__.py   # 启动入口（读取 WEB_HOST/WEB_PORT）
  logging_config.py  # JSON 日志
alembic/        # 数据库迁移脚本
tests/          # 单元测试
docs/           # 运行手册、隐私告知、架构决策记录
```

### 已有业务目录与待补能力

```text
app/adapters/     # 已有QQ官方适配与OpenAI-compatible远程AI适配；NapCat主通道适配待T-305/T-306/T-307补齐
app/moderation/   # 已有文字、图片/GIF、视频/语音/文件、动态规则、AI软证据与反馈学习
app/cases/        # 已有违规历史、案件和审批状态机
app/reports/      # 已有日报、周报和数据清理构建器
app/runtime/      # WebSocket影子运行器与处理流水线
app/web/          # 已有服务端管理后台、CSRF、动态规则和反馈学习页面
tests/fixtures/   # 脱敏QQ事件和媒体/模型固定样本
```

## 关键配置开关

- `ACTION_MODE=SHADOW`：默认，只记录；`OFFICIAL` 才会调用QQ官方撤回/禁言/警告，且必须满足生产校验。
- 上述 `OFFICIAL` 是已有官方Adapter的专用模式，不代表NapCat已支持真实动作；NapCat的provider路由和按群影子/实时开关尚待T-305/T-307实现。
- `EMERGENCY_STOP=false`：急停开关；为 `true` 时禁止进入 `OFFICIAL`。
- `AI_ENABLED=false`：远程AI默认关闭。
- `AI_ENABLED_GROUPS=`：远程AI必须按群显式启用，例如填 `GROUP_OPENID_A,GROUP_OPENID_B`，或在测试环境用 `*`。
- `AI_BASE_URL` / `AI_API_KEY` / `AI_TEXT_MODEL` / `AI_VISION_MODEL`：OpenAI-compatible/MiMo类接口配置；真实密钥只写本地 `.env` 或系统凭据。

## 安全与隐私

- 管理接口只监听本机或可信内网，必须鉴权，禁止无保护暴露公网。
- 真实凭据（API Key、Token、密码、Cookie、私钥）只通过环境变量或系统凭据存储，绝不写入源码、Markdown、测试、日志或 Git 历史。
- 群消息不被当作系统指令、代码、工具参数或提示词执行。
- 上传文件一律视为不可信数据，只解析允许的安全格式，不执行文件、宏、脚本或压缩包内容。
- 群成员内容按最小必要原则处理，样本必须脱敏。
- 远程AI默认关闭并按群启用；模型只返回结构化建议，不能直接撤回、禁言或踢人。
- 管理员反馈只生成候选规则，必须回放、预览和人工发布后才生效。

## 故障排查

- 端口被占用：修改 `.env` 中的 `WEB_PORT`，并用 `uv run python -m app` 启动（该入口会读取 `WEB_HOST`/`WEB_PORT`）。
- 数据库未初始化：应用启动会校验 Alembic 迁移，需先运行 `uv run alembic upgrade head`。
- 数据库文件位置：默认 `data/moderation.db`（SQLite WAL）。
- 日志：默认输出结构化 JSON 到标准输出，级别由 `LOG_LEVEL` 控制（可选 `DEBUG/INFO/WARNING/ERROR/CRITICAL`）。
- 生产环境（`APP_ENV=prod`）必须设置非空 `ADMIN_PASSWORD`，否则拒绝启动。

## Windows 24×7运行

当前仓库尚未完成Windows Service、自启动和重启恢复实现。部署前必须完成 `NEXT_TASKS.md` 的 `T-404`，并按照 [`docs/windows-operations.md`](docs/windows-operations.md) 验证电源、启动、更新、状态恢复、监控和备份流程。

屏幕可以关闭并锁屏，但主机不能进入睡眠或休眠。开发命令 `uv run uvicorn app.main:app --reload` 只用于开发，不得作为生产运行方式；生产运行使用 `uv run python -m app` 或由 Windows Service 管理。

项目已确定使用一台空白Windows电脑进行正式整机测试。W0基础兼容性已通过；新路线为：R-104/T-305/T-306后进入W1 NapCat影子接收，W2验证完整影子闭环，T-307后进入W3隔离群管理动作，T-404后执行W4无人值守恢复，最后才进入W5目标群分阶段上线。详细进入条件见 [`docs/windows-operations.md`](docs/windows-operations.md) 的“分阶段测试计划”。
