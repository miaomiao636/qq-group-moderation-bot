# Agent交接记录

## 日期

2026-09-04

## 当前任务

R-101已由主审复验通过。下一步为 W0（Windows基础兼容）、T-001（QQ官方能力验证）与 T-002（群规与样本准备）。

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

### 三轮复验整改（R-101 复验项 9-10）

- **复验项9**：修复相对SQLite路径依赖当前工作目录。`app/config.py` 新增 `_normalize_sqlite_url`，在配置层把相对路径（含 `./` 与不含 `./`）统一解析到 `PROJECT_ROOT` 下；绝对路径（Unix/Windows 正反斜杠）与 `:memory:` 保持不变。`Settings` 加载时自动规范化，迁移与启动从任意工作目录连接同一数据库。
- **复验项10**：新增 `tests/test_sqlite_path.py` 回归测试，覆盖 README 默认配置、非项目工作目录启动、Windows 绝对路径（正/反斜杠）、Unix 绝对路径、`:memory:`、非 SQLite URL，以及迁移使用规范化绝对路径的端到端校验。

### 四轮复验整改（提交 `7fca851` 主审未通过后的整改）

- **整改A（测试安全）**：三轮版本的 `tests/test_sqlite_path.py` 会删除真实 `PROJECT_ROOT/data/moderation.db`，属于危险测试。现已重写为完全使用 pytest `tmp_path`，测试代码不再出现任何对项目数据目录的写/删操作；新增模块级守卫夹具 `_guard_real_data_dir`，对真实数据目录做前后内容快照（文件名+SHA256），被触碰即断言失败；端到端测试预创建合法 SQLite 哨兵库（含哨兵表），验证迁移不删除、不替换预存在数据库。
- **整改B（端到端执行真实 Alembic）**：三轮版本的"端到端迁移测试"未执行 Alembic。现已修复 `alembic.ini` 相对路径问题：`script_location = %(here)s/alembic`、`prepend_sys_path = %(here)s`，使 Alembic CLI 可从任意工作目录执行。新增子进程测试：从非项目目录执行真实 `alembic upgrade head`（校验 alembic_version 等于 head），再从另一个非项目目录以子进程启动应用并轮询 `/healthz`，确认连接同一数据库；另含未迁移空库拒绝启动的子进程测试。
- **整改C（临时目录）**：`tempfile.mkdtemp(prefix="qqbot-cwd-")` 改为 pytest `tmp_path`，测试后无 `qqbot-cwd-*` 残留。
- **整改D（Windows盘符相对路径）**：`C:relative\db.db` 这类盘符相对路径依赖各盘符的当前工作目录，不可靠。`_normalize_sqlite_url` 现在明确拒绝该形式并给出可操作的错误信息；`C:/...` 与 `C:\...` 绝对路径仍原样保留。
- **整改E（文档状态）**：修正 `PROGRESS.md`、`HANDOFF.md`、`NEXT_TASKS.md`，不再表述"仅剩CI证据"；如实记录主审未通过与整改范围。
- **整改F（完整验证）**：在干净临时副本（含哨兵 `data/moderation.db`）中运行全部验证，详见"验证结果"。
- **整改G（远程CI证据）**：创建私有远程仓库 `miaomiao636/qq-group-moderation-bot` 并推送。首次真实Windows CI暴露 `alembic.ini` 中文注释在cp1252编码下解码失败的问题，已修复（ini改为ASCII注释 + CI强制 `PYTHONUTF8=1`）。提交 `0e0dd73` 的三个CI任务全部真实成功，详见"验证结果"。

### Windows CI 首次真实运行暴露并修复的问题

- Windows runner 默认 locale 为 cp1252，configparser 按 locale 编码读取含中文注释（UTF-8字节）的 `alembic.ini`，`UnicodeDecodeError` 导致所有测试 setup 失败。
- 修复：`alembic.ini` 注释改为 ASCII；CI quality 任务设置 `PYTHONUTF8=1`。该修复同时保护 Windows 生产部署时 `get_head_revision()` 读取 `alembic.ini` 的路径。

## 修改文件

- 新增：`app/__main__.py`、`tests/test_sqlite_path.py`。
- 修改：`pyproject.toml`、`uv.lock`、`app/config.py`、`app/db.py`、`app/main.py`、`tests/conftest.py`、`tests/test_health.py`、`alembic/env.py`、`alembic/script.py.mako`、`alembic/versions/3a9c0c662c2e_init_system_meta.py`、`alembic.ini`、`.github/workflows/ci.yml`、`.env.example`、`.gitignore`、`README.md`、`AGENTS.md`、`DECISIONS.md`、`PROGRESS.md`、`NEXT_TASKS.md`、`HANDOFF.md`、`docs/windows-operations.md`。

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
- **SQLite相对路径**：`_normalize_sqlite_url` 将 README 默认 `sqlite+aiosqlite:///./data/moderation.db` 解析为 `PROJECT_ROOT/data/moderation.db`；从非项目工作目录解析结果一致；Windows 绝对路径（正/反斜杠）、Unix 绝对路径、`:memory:`、非 SQLite URL 均保持不变；Windows 盘符相对路径 `C:relative\db.db` 被明确拒绝。
- **SQLite路径回归测试**：`tests/test_sqlite_path.py` 13项全部通过；`uv run pytest` 共 24 passed。

### 四轮整改验证（在干净临时副本中执行，副本含哨兵 `data/moderation.db`）

- **测试安全**：24 项 pytest 全部通过；测试前后哨兵数据库 SHA256 完全一致（`48d998f0d8a7c370`），哨兵表保留，证明测试未触碰、未删除、未替换真实数据目录文件。
- **临时目录**：测试后无 `qqbot-cwd-*`、`qqbot-test-*` 残留。
- **静态检查**：ruff check 与 format、mypy（7 个源文件）全部通过。
- **Alembic 升降级**：全新库升级到 head、降级到 base、再升级到 head 均成功。
- **构建**：`uv build` 源码包和 wheel 构建成功。
- **配置拒绝**：生产空白密码、非法日志级别、Windows 盘符相对路径均被拒绝启动。
- **实际端口**：`WEB_PORT=8133` 启动后 `/healthz` 在该端口返回成功。
- **运行时依赖**：干净 `uv sync --no-dev` 后 `uv run --no-sync` 导入 `app.main` 成功，pytest 不可导入。
- **Git**：基线提交 `688a5da` 已建立，修复修改可审计。

### 远程 CI 真实运行证据（复验项11，提交 `0e0dd73`）

- 远程仓库：`https://github.com/miaomiao636/qq-group-moderation-bot`（私有）。
- CI 运行：`https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33877462326`（结论 success，head 提交 `0e0dd73`）。
- Ubuntu质量：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33877462326/job/101037748579 ✓
- Windows质量：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33877462326/job/101037748749 ✓
- 运行时依赖回归：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33877462326/job/101037748778 ✓

## 遗留问题

- **R-101已通过**：四轮整改与远程CI取证（复验项11）均已完成并获主审确认，无遗留阻塞项。
- **W0 阻塞**：需要项目负责人提供可用的空白Windows电脑（含系统版本、CPU架构、补丁状态记录）与测试网络；当前Mac环境无法执行。
- **T-001 阻塞**：需要项目负责人提供QQ官方应用、隔离测试群与全量消息/撤回/禁言权限。
- **T-002 阻塞**：需要项目负责人提供群规、白名单与脱敏样本。
- Windows 真实运行、自启、重启和更新恢复需在 Windows 专用机通过 T-404 演练验证，当前 Mac 环境无法验证。
- T-404 只有文档和验收标准，尚无实际实现与实机证据。

## 下一步建议

1. 请项目负责人提供 W0 所需 Windows 电脑、T-001 所需 QQ 官方应用/隔离群/权限、T-002 所需群规/白名单/脱敏样本。
2. 获得上述资源后，依次推进 W0、T-001、T-002；T-102 依赖 T-001，须在 T-001 完成后才可开始。
3. 完整Windows阶段和门槛见 `docs/windows-operations.md`。

## 主审复验结论

### 对提交 `8f3ea0f` 的复验

- 已通过：`uv sync --locked --all-groups`、11项pytest、mypy、ruff检查与格式、构建、锁文件、依赖兼容、干净运行时依赖、Alembic升级/降级/head校验、配置拒绝、实际 `WEB_PORT`、不在项目目录时的Alembic脚本定位。
- 未通过：README默认相对SQLite配置的目录稳定性；远程Linux/Windows CI真实运行。
- 决定：R-101不通过，T-102暂不放行。

### 对提交 `7fca851` 的复验

- 未通过，理由：
  1. `tests/test_sqlite_path.py` 会删除真实 `PROJECT_ROOT/data/moderation.db`，属于危险测试。
  2. 所谓端到端迁移测试没有执行 Alembic，验证力度不足。
  3. 测试使用 `tempfile.mkdtemp(prefix="qqbot-cwd-")`，存在残留风险。
  4. Windows 盘符相对路径 `C:relative\db.db` 未处理（既未规范化也未拒绝）。
  5. 文档错误表述"仅剩CI证据"。
- 决定：R-101继续不通过，T-102暂不放行。

### 四轮整改后状态（R-101已通过）

- 提交 `7fca851` 复验提出的缺陷已全部整改：测试完全使用 `tmp_path` 并带真实数据目录守卫夹具；`alembic.ini` 使用 `%(here)s` 并新增子进程真实 Alembic 升级与跨目录启动的端到端测试；盘符相对路径明确拒绝；文档状态已修正。
- 干净临时副本（含哨兵真实数据库）中完成全部验证：24项pytest、静态检查、Alembic升降级、构建、配置拒绝、实际端口、运行时依赖；哨兵数据库字节级未变。
- 复验项11已完成：私有远程仓库已建立，提交 `0e0dd73` 的 Ubuntu质量、Windows质量、运行时依赖三个CI任务真实成功（链接见"验证结果"）。
- **主审复验结论：R-101通过。**
