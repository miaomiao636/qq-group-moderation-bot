# 项目进度

## 当前阶段

脚手架整改阶段。T-003已交付；T-101已交付，R-101整改已完成并提交，等待主审Agent复验。业务代码尚未开始。

## 已完成

- **T-003 固化首批技术决策**（2026-09-03）：
  - 在 `DECISIONS.md` 记录D-001至D-005：主通道与执行出口、技术栈与运行环境、依赖管理/数据库迁移/质量门禁、配置命名与运行模式、多模态与模型接入原则。
- **T-101 项目脚手架与质量门禁（已交付）**（2026-09-03）：
  - 创建Python 3.12项目、FastAPI应用入口、配置、SQLite WAL、JSON日志、ORM占位模型、Alembic初始迁移、基础测试和Linux CI。
- **R-101 T-101主审整改（已完成，待复验）**（2026-09-04）：
  - 将 `aiosqlite` 移入运行时依赖，干净生产环境可导入和启动。
  - 取消 `Base.metadata.create_all`，Alembic 成为唯一生产建表路径，启动时校验迁移版本。
  - 为 `APP_ENV`/`RUN_MODE`/端口/保留期/日志级别增加校验，生产环境拒绝空管理员密码。
  - 新增 `app/__main__.py` 启动入口，读取 `WEB_HOST`/`WEB_PORT`。
  - 将 `alembic/` 纳入 ruff 检查并修复问题。
  - CI 使用锁文件安装并增加 Windows 测试环境。
  - 测试数据库使用临时目录隔离。
  - 修正 README 规划目录与实际目录混淆。
  - 处理 TestClient 弃用警告（安装 `httpx2`、锁定 `anyio` 警告、修复 Alembic `path_separator`）。
  - 初始化 Git 仓库并建立基线提交。
- **R-101 复验整改（已完成，待复验）**（2026-09-04）：
  - 数据库校验必须等于当前代码的 Alembic head 版本，拒绝 `stale_revision` 等过期版本。
  - 生产环境仅含空白字符的密码被拒绝（`strip` 后非空）。
  - CI 新增 `runtime-deps` 回归检查 job，仅安装运行时依赖并验证 `import app.main`。
  - 测试临时目录在会话结束后主动删除，不残留 `qqbot-test-*`/`qqbot-nomigrate-*`。
  - 修正 `NEXT_TASKS.md` 状态矛盾与 `HANDOFF.md` 基线提交号。
- **R-101 复验整改二轮（已完成，待复验）**（2026-09-04）：
  - 修复运行时依赖 CI 失效：`uv run` 会自动重装 dev 依赖，改用 `--no-sync` 并断言 pytest 不可导入。
  - 修复非项目工作目录无法启动：`get_head_revision` 基于 `PROJECT_ROOT` 解析 `alembic.ini` 与 `script_location`。
  - 修复 Windows 清理风险：删除临时目录前关闭全局数据库引擎，移除 `ignore_errors=True`。
- **Windows 24×7运行与恢复需求补充**（2026-09-04）：
  - 新增 `docs/windows-operations.md`，并在项目上下文、决策、Agent规则、任务和README中同步恢复机制。
  - 新增T-404，覆盖Windows Service、自启动、状态恢复、更新维护、健康检查、备份和NapCat人工回退。

## 进行中

- **R-101 T-101主审整改**：实现已完成并验证通过，等待主审Agent复验；复验通过前不开始T-102。
- **T-404 Windows无人值守运行与灾难恢复**：仅完成需求和验收标准，尚未实现或在Windows实机演练。

## 已知问题

- 无阻塞性问题。
- **Windows CI 无真实运行证据**：工作流已配置 Linux+Windows 矩阵，但当前仓库无远程地址，无法在 GitHub Actions 中产生 Windows 真实运行证据；需推送远程仓库后由主审Agent确认。
- 注意：`anyio.abc.BlockingPortal` 弃用警告来自 starlette 库，已在 pytest 配置中锁定。
- 注意：Windows 真实运行、自启动、重启和更新恢复需在 Windows 专用机通过 T-404 演练验证，当前 Mac 环境无法验证。
- 尚无QQ 官方适配器、机器人引擎、案件、审批、报告或NapCat实现。

## 最近更新

日期：2026-09-04

修改内容：完成 R-101 全部整改项及两轮复验项，并通过干净生产安装、Alembic 迁移（含 head 版本校验）、配置校验、端口生效、CI 配置（含 runtime-deps 回归）、非项目工作目录启动和 Git 基线验证。

影响：脚手架达到可部署、可审核、可回退状态；R-101 复验通过后可开始 T-102。