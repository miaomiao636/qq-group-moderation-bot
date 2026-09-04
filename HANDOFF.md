# Agent交接记录

## 日期

2026-09-04

## 当前任务

R-101 T-101主审整改（含复验项）。

## 已完成内容

### 首轮整改（R-101）

- **整改项1**：将 `aiosqlite` 从开发依赖移入运行时依赖；干净生产环境（不带 dev 组）可导入 `app.main` 并启动。
- **整改项2**：取消 `Base.metadata.create_all`；新增 `check_db_migrated()` 校验数据库已通过 Alembic 迁移，未迁移则拒绝启动。
- **整改项3**：为 `APP_ENV`/`RUN_MODE`/`WEB_PORT`/保留天数/`LOG_LEVEL` 增加类型与范围校验；生产环境拒绝空管理员密码。
- **整改项4**：新增 `app/__main__.py` 启动入口，读取 `WEB_HOST`/`WEB_PORT`；`uv run python -m app` 启动时端口生效。
- **整改项5**：将 `alembic/` 纳入 ruff 检查与格式检查，修复迁移文件尾随空格与导入顺序问题。
- **整改项6**：CI 使用 `uv sync --locked` 锁文件安装，新增 Windows 测试环境（Linux + Windows 矩阵）。
- **整改项7**：测试数据库改用 `tempfile.mkdtemp` 临时目录隔离，不再使用固定 `tests/test_data/test.db`。
- **整改项8**：修正 README 目录结构，区分"当前实际存在"与"规划中"目录。
- **整改项9**：安装 `httpx2` 解决 TestClient 的 httpx 弃用警告；锁定 `anyio` 内部警告；修复 Alembic `path_separator` 警告。
- **整改项10**：初始化 Git 仓库，建立整改前基线提交 `688a5da`。

### 复验整改（R-101 复验项）

- **复验项1**：`check_db_migrated()` 现在校验数据库版本必须等于当前代码的 Alembic head（`3a9c0c662c2e`），拒绝 `stale_revision` 等过期版本，防止旧数据库结构直接运行新代码。
- **复验项2**：生产环境密码使用 `strip()` 后校验，仅含空白字符的密码被拒绝。
- **复验项3**：CI 新增 `runtime-deps` 回归检查 job，仅安装运行时依赖并验证 `import app.main`，防止运行时依赖被误放入开发组。
- **复验项4**：Windows CI 已配置 Linux+Windows 矩阵，但当前仓库无远程地址，无法在 GitHub Actions 产生 Windows 真实运行证据；需推送远程仓库后由主审Agent确认。
- **复验项5**：测试临时目录在会话结束后主动删除（`shutil.rmtree`），不再残留 `qqbot-test-*`/`qqbot-nomigrate-*`。

### 二轮复验整改（R-101 复验项 6-8）

- **复验项6**：修复运行时依赖 CI 失效。`uv run` 默认会自动重新同步项目环境（含 dev 组），导致 `uv sync --no-dev` 后 dev 依赖被悄悄装回。改用 `uv run --no-sync` 阻止自动重装，并新增反向断言：dev 依赖（pytest）在干净运行时环境中必须不可导入。
- **复验项7**：修复非项目工作目录无法启动。`get_head_revision()` 原用相对路径 `Config("alembic.ini")`，切换工作目录后报 `No 'script_location' key found`。现基于 `PROJECT_ROOT` 解析 `alembic.ini`，并将 `script_location` 设为项目根目录的绝对路径，从任意工作目录启动均可定位迁移脚本。
- **复验项8**：修复 Windows 清理风险。删除测试临时目录前先关闭全局数据库引擎（`engine.dispose()`），避免 Windows 上 SQLite 文件被占用无法删除；移除 `ignore_errors=True`，删除失败显式暴露，不再隐藏。

## 修改文件

- 新增：`app/__main__.py`。
- 修改：`pyproject.toml`、`uv.lock`、`app/config.py`、`app/db.py`、`app/main.py`、`tests/conftest.py`、`tests/test_health.py`、`alembic/env.py`、`alembic/script.py.mako`、`alembic/versions/3a9c0c662c2e_init_system_meta.py`、`alembic.ini`、`.github/workflows/ci.yml`、`.env.example`、`.gitignore`、`README.md`、`AGENTS.md`、`DECISIONS.md`、`PROGRESS.md`、`NEXT_TASKS.md`、`HANDOFF.md`。

## 验证结果

- 干净生产依赖安装（仅运行时）：`import app.main` 成功，`aiosqlite` 已安装，`pytest` 不在运行时环境。
- 干净生产环境启动：`uv run python -m app` 启动成功，健康检查返回 `{"status":"ok","env":"local","mode":"SAFE"}`。
- `uv run pytest`：11 passed，无警告。
- `uv run mypy app`：Success, no issues found in 7 source files。
- `uv run ruff check app tests alembic`：All checks passed。
- `uv run ruff format --check app tests alembic`：12 files already formatted。
- Alembic 全新数据库升级：成功，`alembic_version` 版本为 `3a9c0c662c2e`。
- Alembic 降级后重新升级：成功。
- 未迁移数据库启动：被拒绝，抛出 `RuntimeError: 数据库未通过 Alembic 迁移`。
- **过期版本拒绝**：`stale_revision` 被拒绝，错误信息提示需 `alembic upgrade head`。
- **head 版本匹配**：迁移到 head 的数据库通过 `check_db_migrated()`。
- **空白密码拒绝**：生产环境 `ADMIN_PASSWORD="   "` 被拒绝；正常密码通过。
- 无效配置拒绝启动：非法 `RUN_MODE`/越界端口/负数保留期/非法日志级别/生产空密码均被拒绝。
- `WEB_PORT` 实际生效：`WEB_PORT=8125`/`8127` 启动后健康检查在对应端口返回成功。
- CI 配置：YAML 语法正确，`quality`（Linux+Windows 矩阵）+ `runtime-deps` 两个 job。
- `uv sync --locked --all-groups`：通过。
- `uv build`：源码包和 wheel 构建成功。
- 敏感信息扫描：无泄漏。
- 测试临时目录清理：测试后无残留 `qqbot-test-*`/`qqbot-nomigrate-*` 目录。
- **运行时依赖 CI 回归**：干净 `uv sync --no-dev` 后，`uv run --no-sync` 导入 `app.main` 成功，pytest 不可导入（dev 依赖未装回）。
- **非项目工作目录启动**：从 `/tmp` 调用 `get_head_revision()` 返回 `3a9c0c662c2e`；从 `/tmp` 启动应用健康检查返回 `{"status":"ok","env":"local","mode":"SAFE"}`。
- **Windows 清理**：测试后临时目录被主动删除，删除前关闭全局数据库引擎，无 `ignore_errors` 隐藏。
- Git：基线提交 `688a5da` 已建立，修复修改可审核。

## 遗留问题

- **Windows CI 无真实运行证据**：工作流已配置 Linux+Windows 矩阵，但当前仓库无远程地址，无法在 GitHub Actions 中产生 Windows 真实运行证据。需在推送远程仓库后，由主审Agent确认 Windows job 实际通过。
- Windows 真实运行、自启动、重启和更新恢复需在 Windows 专用机通过 T-404 演练验证，当前 Mac 环境无法验证。
- T-001 和 T-002 仍需要项目负责人提供 QQ 官方应用、隔离测试群、群规与脱敏样本。
- T-404 只有需求和验收标准，没有 Windows 实现与实机证据。

## 下一步建议

1. 由主审Agent复验 R-101 的干净安装、迁移（含 head 版本校验）、配置、端口、CI（含 runtime-deps 回归）和 Git 基线。
2. 将仓库推送到远程 GitHub 仓库，以产生 Windows CI 真实运行证据。
3. R-101 复验通过后，开始 T-102（统一消息契约与官方机器人适配器），需 T-001 提供 QQ 官方应用与测试群。
4. 项目负责人并行准备 T-001 所需 QQ 官方应用和测试群，以及 T-002 所需群规和脱敏样本。