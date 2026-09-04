# 项目进度

## 当前阶段

脚手架整改复验阶段。T-003与T-101已交付，**R-101已由主审复验通过**。下一步为 W0（Windows基础兼容）、T-001（QQ官方能力验证）与 T-002（群规与样本准备）。业务代码尚未开始。

## 已完成

- **T-003 固化首批技术决策**（2026-09-03）：
  - 在 `DECISIONS.md` 记录D-001至D-005：主通道与执行出口、技术栈与运行环境、依赖管理/数据库迁移/质量门禁、配置命名与运行模式、多模态与模型接入原则。
- **T-101 项目脚手架与质量门禁（已交付）**（2026-09-03）：
  - 创建Python 3.12项目、FastAPI应用入口、配置、SQLite WAL、JSON日志、ORM占位模型、Alembic初始迁移、基础测试和Linux CI。
- **R-101 T-101主审整改首轮实现（已提交，主审未通过）**（2026-09-04）：
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
- **R-101 复验整改首轮实现（已提交，主审未通过）**（2026-09-04）：
  - 数据库校验必须等于当前代码的 Alembic head 版本，拒绝 `stale_revision` 等过期版本。
  - 生产环境仅含空白字符的密码被拒绝（`strip` 后非空）。
  - CI 新增 `runtime-deps` 回归检查 job，仅安装运行时依赖并验证 `import app.main`。
  - 测试临时目录在会话结束后主动删除，不残留 `qqbot-test-*`/`qqbot-nomigrate-*`。
  - 修正 `NEXT_TASKS.md` 状态矛盾与 `HANDOFF.md` 基线提交号。
- **R-101 复验整改二轮实现（局部通过，整体主审未通过）**（2026-09-04）：
  - 修复运行时依赖 CI 失效：`uv run` 会自动重装 dev 依赖，改用 `--no-sync` 并断言 pytest 不可导入。
  - 修复非项目工作目录无法启动：`get_head_revision` 基于 `PROJECT_ROOT` 解析 `alembic.ini` 与 `script_location`。
  - 修复 Windows 清理风险：删除临时目录前关闭全局数据库引擎，移除 `ignore_errors=True`。
- **R-101 复验整改三轮实现（SQLite相对路径，主审未通过）**（2026-09-04，提交 `7fca851`）：
  - `app/config.py` 新增 `_normalize_sqlite_url`，在配置层把相对SQLite路径统一解析到 `PROJECT_ROOT` 下。
  - `tests/test_sqlite_path.py` 首版回归测试。主审复验发现缺陷：测试会删除真实 `data/moderation.db`、端到端测试未执行Alembic、使用 `tempfile.mkdtemp` 有残留风险、Windows盘符相对路径未处理。
- **R-101 复验整改四轮实现（测试安全与端到端修复，已完成待复验）**（2026-09-04）：
  - `tests/test_sqlite_path.py` 重写为完全使用 pytest `tmp_path`，新增真实数据目录守卫夹具（前后内容快照，被触碰即失败）；预创建哨兵数据库验证迁移不删除、不替换预存在文件。
  - `alembic.ini` 改用 `%(here)s` 解析 `script_location` 与 `prepend_sys_path`，Alembic CLI 可从任意工作目录执行；新增子进程测试：从非项目目录执行真实 `alembic upgrade head`，再从另一目录启动应用确认连接同一数据库；含未迁移空库拒绝启动测试。
  - Windows 盘符相对路径 `C:relative\db.db` 被明确拒绝（依赖各盘符当前目录，行为不可靠）。
  - 文档状态修正，不再表述"仅剩CI证据"。
  - 干净临时副本（含哨兵 `data/moderation.db`）全量验证：24项pytest、静态检查、Alembic升降级、构建、配置拒绝、实际端口、运行时依赖；哨兵数据库字节级未变。
- **R-101 复验整改四轮补充（Windows CI 编码修复与远程CI取证，已完成）**（2026-09-04）：
  - 首次真实Windows CI暴露：Windows runner 默认 cp1252 编码读取含中文注释的 `alembic.ini` 导致 `UnicodeDecodeError`；修复为 ini 注释 ASCII 化并在 CI 强制 `PYTHONUTF8=1`，同时保护 Windows 生产部署读取 ini 的路径。
  - 创建私有远程仓库 `miaomiao636/qq-group-moderation-bot` 并推送；提交 `0e0dd73` 的 Ubuntu质量、Windows质量、运行时依赖回归三个 CI 任务全部真实成功。
- **Windows 24×7运行与恢复需求补充**（2026-09-04）：
  - 新增 `docs/windows-operations.md`，并在项目上下文、决策、Agent规则、任务和README中同步恢复机制。
  - 新增T-404，覆盖Windows Service、自启动、状态恢复、更新维护、健康检查、备份和NapCat人工回退。

## 进行中

- **R-101 T-101主审整改**：已由主审复验通过（复验项1至11全部完成）。
- **W0 Windows基础兼容**：R-101通过后开始，在空白Windows电脑跑干净安装、迁移和全部质量命令，建立Windows基线。
- **T-001 QQ官方能力验证**：待项目负责人提供QQ官方应用、隔离测试群与权限后执行。
- **T-002 群规与样本准备**：待项目负责人提供群规、白名单与脱敏样本后执行。
- **T-404 Windows无人值守运行与故障恢复**：仅完成需求和验收标准，尚未实现或在Windows实机演练。
- **Windows正式测试环境**：项目负责人已准备一台Windows电脑；尚未建立W0基线。

## 已知问题

- **Windows CI 编码问题已修复**：Windows runner cp1252 编码读取含中文注释的 `alembic.ini` 会报 `UnicodeDecodeError`；现已 ASCII 化并在 CI 强制 `PYTHONUTF8=1`。
- **SQLite相对路径**：配置层规范化（`_normalize_sqlite_url`）与13项回归测试已就位；Windows盘符相对路径被明确拒绝；测试使用 `tmp_path` 并带真实数据目录守卫，不触碰真实数据库。
- **远程CI证据**：提交 `0e0dd73` 三个任务真实成功，运行链接记录于 `HANDOFF.md`。
- 注意：`anyio.abc.BlockingPortal` 弃用警告来自 starlette 库，已在 pytest 配置中锁定。
- 注意：Windows 真实运行、自启动、重启和更新恢复需在 Windows 专用机通过 T-404 演练验证，当前 Mac 环境无法验证。
- 尚无QQ 官方适配器、机器人引擎、案件、审批、报告或NapCat实现。

## 最近更新

日期：2026-09-04

修改内容：**R-101已由主审复验通过**。四轮整改（测试安全、真实Alembic端到端、盘符相对路径、文档状态、Windows CI编码修复）与远程CI取证（复验项11）全部完成并获主审确认。文档状态统一更新为"R-101已通过"。

影响：下一步为 W0（Windows基础兼容）、T-001（QQ官方能力验证）与 T-002（群规与样本准备）。T-102依赖T-001，须在T-001完成后才可开始。
