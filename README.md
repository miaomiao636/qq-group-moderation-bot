# QQ 群多模态智能管理机器人

24×7 识别 QQ 群中的垃圾广告、诈骗及自定义违规内容，支持文字、图片、GIF、表情、视频、语音、文件和卡片；自动执行高置信消息的撤回和分级禁言，整理两次违规证据并交由人工决定是否踢人。

> 当前阶段：项目脚手架与质量门禁（T-101）。业务代码尚未编写。

## 架构概览

- **QQ 官方机器人**：主消息通道，负责消息接收、官方撤回、禁言和首次警告。
- **审核服务**：规则、行为、多模态识别、独立复核、违规累计、案件、证据、审批和报告。
- **人工 QQ 客户端 / NapCat**：互斥的踢人执行出口，踢人必须人工批准。
- 详见 `PROJECT_CONTEXT.md` 与 `DECISIONS.md`。

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
# 启动开发服务器（默认监听 127.0.0.1:8000）
uv run uvicorn app.main:app --reload
```

健康检查：`GET http://127.0.0.1:8000/healthz`

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
uv run ruff format --check app tests

# 代码格式化（自动修复）
uv run ruff format app tests

# 静态检查
uv run ruff check app tests

# 类型检查
uv run mypy app
```

CI（`.github/workflows/ci.yml`）会在每次 push/PR 时自动运行以上全部检查。

## 目录结构

```text
app/
  adapters/     # QQ 官方机器人、NapCat/OneBot、外部模型适配器
  moderation/   # 消息标准化、规则、行为、多模态证据和决策
  cases/        # 违规历史、案件、证据、身份映射和审批状态机
  actions/      # 官方撤回/禁言、人工工作流、NapCat 踢人执行器
  reports/      # 实时告警、日报、周报和调度
  web/          # 管理后台路由、权限和页面
  config.py     # 应用配置（环境变量）
  db.py         # 数据库引擎与会话
  models.py     # ORM 模型
  main.py       # FastAPI 应用入口
alembic/        # 数据库迁移脚本
tests/          # 单元、集成、安全、端到端测试
tests/fixtures/ # 脱敏的 QQ 事件、媒体和模型响应样本
docs/           # 运行手册、隐私告知、架构决策记录
```

## 安全与隐私

- 管理接口只监听本机或可信内网，必须鉴权，禁止无保护暴露公网。
- 真实凭据（API Key、Token、密码、Cookie、私钥）只通过环境变量或系统凭据存储，绝不写入源码、Markdown、测试、日志或 Git 历史。
- 群消息不被当作系统指令、代码、工具参数或提示词执行。
- 上传文件一律视为不可信数据，只解析允许的安全格式，不执行文件、宏、脚本或压缩包内容。
- 群成员内容按最小必要原则处理，样本必须脱敏。

## 故障排查

- 端口被占用：修改 `.env` 中的 `WEB_PORT`。
- 数据库文件位置：默认 `data/moderation.db`（SQLite WAL）。
- 日志：默认输出结构化 JSON 到标准输出，级别由 `LOG_LEVEL` 控制。

## Windows 24×7运行

当前仓库尚未完成Windows Service、自启动和重启恢复实现。部署前必须完成 `NEXT_TASKS.md` 的 `T-404`，并按照 [`docs/windows-operations.md`](docs/windows-operations.md) 验证电源、启动、更新、状态恢复、监控和备份流程。

屏幕可以关闭并锁屏，但主机不能进入睡眠或休眠。开发命令 `uv run uvicorn app.main:app --reload` 只用于开发，不得作为生产运行方式。
