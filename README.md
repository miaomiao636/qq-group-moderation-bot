# QQ 群多模态智能管理机器人

24×7 识别 QQ 群中的垃圾广告、诈骗及自定义违规内容，支持文字、图片、GIF、表情、视频、语音、文件和卡片；自动执行高置信消息的撤回和分级禁言，整理两次违规证据并交由人工决定是否踢人。

> 当前阶段（2026-09-10）：PR #5实现T-307与R-105安全整改；默认SHADOW、OneBot真实动作关闭。代码合并不表示Windows验收通过。部署人员先读[Windows实测与交付清单](docs/windows-delivery-checklist.md)，按W1→W2→隔离群W3→恢复W4→小规模W5执行；实际本轮门禁见PROGRESS/PR。

## 架构概览

- **NapCatQQ + OneBot 11**：主通道；持久接收箱、账号绑定、撤回/禁言/警告Adapter已实现，真实效果待Windows验证。
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

该入口需要本地配置QQ官方凭据。默认 `ACTION_MODE=SHADOW`，只记录审核建议和模拟动作，不执行撤回、禁言或警告。

NapCat反向WebSocket由上面的Web服务接收：在 `.env` 显式设置 `ONEBOT_WS_ENABLED=true` 与强令牌，把NapCat反向WS地址配置为 `ws://<WEB_HOST>:<WEB_PORT>/onebot/ws`，并让NapCat使用同一令牌发送 `Authorization: Bearer` 请求头。令牌不允许放在URL查询参数中。`/onebot/status` 同样需要Bearer令牌；公开的 `/healthz` 只返回不含QQ号、群号和内部错误的聚合就绪状态。Windows正式安装、版本固定和自启动仍按T-303/T-404完成。

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

> 质量记录按提交核对：T-306历史CI不能替代R-105。本轮结果见PROGRESS与PR #5；真实模型、QQ操作、Windows恢复不由mock单测证明。新增迁移head为 `c2e4f6a8b010`，升级后历史按群动作位安全关闭，不能自动恢复。

## 目录结构

### 当前实际存在的目录

```text
app/            # 应用包
  actions/      # provider中立撤回/禁言/警告编排（默认SHADOW）
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
app/adapters/     # QQ官方、OneBot入站/动作与供应商兼容AI适配
app/core/         # 传输中立消息、动作、路由和去重契约
app/moderation/   # 已有文字、图片/GIF、视频/语音/文件、动态规则、AI软证据与反馈学习
app/cases/        # 已有违规历史、案件和审批状态机
app/reports/      # 已有日报、周报和数据清理构建器
app/runtime/      # WebSocket、持久接收箱与处理流水线
app/web/          # 已有服务端管理后台、CSRF、动态规则和反馈学习页面
tests/fixtures/   # 脱敏QQ事件和媒体/模型固定样本
```

## 关键配置开关

- `ACTION_MODE=SHADOW`：默认不执行真实动作；`OFFICIAL`为兼容保留的全局真实动作门禁名，实际Adapter由provider路由选择。OneBot-only无需官方凭据，官方出口仍需凭据。
- OneBot真实动作另需 `ONEBOT_ACTIONS_ENABLED=true`、`ONEBOT_SELF_ID`专用数字账号、就绪连接、明确路由与真人批准按群动作。`ONEBOT_ACTION_STAGE=recall_only`默认仅撤回，负责人在本机改为full并重启才进入禁言/警告阶梯。所有开关全开就可能处罚真实成员，程序不会自动判断你的验收报告是否通过。
- `EMERGENCY_STOP=false`：急停开关；为 `true` 时禁止进入 `OFFICIAL`。
- `AI_ENABLED=false`：远程AI默认关闭。
- `AI_ENABLED_GROUPS=`：远程AI必须按群显式启用，例如填 `GROUP_OPENID_A,GROUP_OPENID_B`，或在测试环境用 `*`。
- `AI_BASE_URL` / `AI_API_KEY` / `AI_TEXT_MODEL` / `AI_VISION_MODEL`：OpenAI-compatible/MiMo类接口配置；真实密钥只写本地 `.env` 或系统凭据。
- `ONEBOT_WS_ENABLED=false`：OneBot反向WS默认关闭；启用时必须设置 `ONEBOT_ACCESS_TOKEN`，并只绑定回环或明确的私有网段地址（通配地址 `0.0.0.0`/`::` 会被拒绝）。
- `ONEBOT_WS_PATH=/onebot/ws`：NapCat连接使用Bearer请求头；URL查询参数令牌被拒绝，避免令牌进入访问日志。
- `AI_REVIEW_MODEL`：必须不同于主视觉模型；只有灰区/冲突/疑难才调用。缺复核或失败只记录，默认不为正常消息调用第二模型。
- `AI_DAILY_CALL_LIMIT=1000`：调用上限不含缓存，主/次分别可查；价格未接入时“未核算”，不要把0当免费。完整配置见 `.env.example`。
- `AGENT_API_READ_TOKEN` / `AGENT_API_TOKEN`：分离只读/有限写令牌，不能复用ADMIN_PASSWORD；`AGENT_API_WRITE_SCOPES=project:read`默认只读。高风险操作保存计划后由真人后台会话+CSRF预览批准，Agent不能自行批准、跨目标执行或重放。
- `ADMIN_SESSION_TTL_SECONDS=3600`：管理员会话有限期；本机HTTP只用回环，非本机访问需TLS或安全隧道，不能靠“内网”代替传输加密。

## AI学习与验收

这不是在线训练系统。人工标注→本地候选（自动挖掘至少3条消息、2名成员）→草稿回放→真人发布→版本回滚；负责人明确指定群规可以直接建规则草稿，不必凑样本数。单条标签不立即变成所有消息的prompt背景，未标注也不当正常。候选只取每条消息最新标签，撤销真值会使未发布候选重新计算/失效，复制前再核验；不擅自回滚已发布规则。

管理后台的“已标注样本一致率”不是独立精确率。离线报告使用 `uv run python -m app.reports.evaluation --input <脱敏JSONL> --output <新的报告JSON>`，样本字段、指标与Windows证据要求见交付清单；此工具只统计，不自动宣告通过。

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

历史实施报告已描述NSSM服务化，但本轮主审未核验Windows原件；按T-404和[交付清单](docs/windows-delivery-checklist.md)重验锁屏/开机登录依赖/断网/进程崩溃/备份恢复。必须单Web/OneBot实例，禁止多worker；SQLite是本轮支持的数据方案。

屏幕可以关闭并锁屏，但主机不能进入睡眠或休眠。开发命令 `uv run uvicorn app.main:app --reload` 只用于开发，不得作为生产运行方式；生产运行使用 `uv run python -m app` 或由 Windows Service 管理。

项目已确定使用一台空白Windows电脑进行正式整机测试。W0基础兼容性已通过；新路线为：R-104/T-305/T-306后进入W1 NapCat影子接收，W2验证完整影子闭环，T-307后进入W3隔离群管理动作，T-404后执行W4无人值守恢复，最后才进入W5目标群分阶段上线。详细进入条件见 [`docs/windows-operations.md`](docs/windows-operations.md) 的“分阶段测试计划”。

现有告警仅后台展示，不是主动推送；无人值守交付还需客户确定接管人及独立通知渠道并实测到达。无法完成时明确按有人值班的辅助工具交付。Windows 10安全支持与补丁也需复核，不擅自更改正式电脑系统。
