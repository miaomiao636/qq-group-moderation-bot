# QQ 群多模态智能管理机器人

24×7 识别 QQ 群中的垃圾广告、诈骗及自定义违规内容，支持文字、图片、GIF、表情、视频、语音、文件和卡片；自动执行高置信消息的撤回和分级禁言，整理两次违规证据并交由人工决定是否踢人。

> 当前阶段：QQ官方适配、影子审核、文字/媒体规则、案件、后台和报告已有实现，但仍只记录不处罚。R-102独立复验发现核心正确性问题，必须先完成R-103；动态规则、远程AI、人工反馈学习和真实官方动作尚未实现或验收。

## 架构概览

- **QQ 官方机器人**：主消息通道，负责消息接收、官方撤回、禁言和首次警告。
- **审核服务**：规则、行为、多模态识别、独立复核、违规累计、案件、证据、审批和报告。
- **人工 QQ 客户端 / NapCat**：互斥的踢人执行出口，踢人必须人工批准。
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

当前QQ常驻运行器只能用于隔离群影子观察：

```bash
uv run python -m app.runtime
```

该入口需要本地配置QQ官方凭据，只记录审核建议，不执行撤回、禁言或警告。R-103完成前不要将它用于真实自动处罚；同步代码或重启也不会自动开启处罚。

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

## 目录结构

### 当前实际存在的目录

```text
app/            # 应用包
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
app/adapters/     # 已有QQ官方适配；远程AI与NapCat适配待补
app/moderation/   # 已有文字、图片/GIF、视频/语音/文件与复核门
app/cases/        # 已有违规历史、案件和审批状态机
app/reports/      # 已有日报、周报和数据清理构建器
app/runtime/      # WebSocket影子运行器与处理流水线
app/web/          # 已有服务端管理后台；动态规则编辑与统一CSRF待补
tests/fixtures/   # 脱敏QQ事件和媒体/模型固定样本
```

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

项目已确定使用一台空白Windows电脑进行正式整机测试。该电脑按W0至W5分阶段投入，不需要等全部功能写完才第一次测试：R-101通过后先做基础兼容性，T-102后测试QQ官方链路，核心业务完成后测试安全模式，T-404后测试无人值守恢复，NapCat只在单独批准后测试。详细进入条件见 [`docs/windows-operations.md`](docs/windows-operations.md) 的“分阶段测试计划”。
