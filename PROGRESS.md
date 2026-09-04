# 项目进度

## 当前阶段

脚手架整改复验阶段。T-003与T-101实现已交付；提交 `7fca851` 主审未通过（危险测试、端到端未执行Alembic、临时目录残留、盘符相对路径未处理、文档状态失实），第四轮整改已完成并通过干净副本全量验证，等待主审复验。业务代码尚未开始。

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
- **Windows 24×7运行与恢复需求补充**（2026-09-04）：
  - 新增 `docs/windows-operations.md`，并在项目上下文、决策、Agent规则、任务和README中同步恢复机制。
  - 新增T-404，覆盖Windows Service、自启动、状态恢复、更新维护、健康检查、备份和NapCat人工回退。

## 进行中

- **R-101 T-101主审整改**：第四轮整改（测试安全、真实Alembic端到端、盘符相对路径、文档状态）已完成并通过干净副本全量验证，等待主审复验；复验项11（真实Linux/Windows CI证据）因无远程仓库暂无法完成。
- **T-404 Windows无人值守运行与灾难恢复**：仅完成需求和验收标准，尚未实现或在Windows实机演练。
- **Windows正式测试环境**：项目负责人已准备一台Windows电脑；尚未建立W0基线，等待R-101阻塞项关闭。

## 已知问题

- **远程仓库未配置**：工作流虽已配置Linux+Windows矩阵，但当前仓库无远程地址，无法产生可核验的CI任务记录；需项目负责人提供私有远程仓库。
- **SQLite相对路径**：配置层规范化（`_normalize_sqlite_url`）与13项回归测试已就位；Windows盘符相对路径被明确拒绝；测试使用 `tmp_path` 并带真实数据目录守卫，不触碰真实数据库。
- 注意：`anyio.abc.BlockingPortal` 弃用警告来自 starlette 库，已在 pytest 配置中锁定。
- 注意：Windows 真实运行、自启动、重启和更新恢复需在 Windows 专用机通过 T-404 演练验证，当前 Mac 环境无法验证。
- 尚无QQ 官方适配器、机器人引擎、案件、审批、报告或NapCat实现。

## 最近更新

日期：2026-09-04

修改内容：主审复验提交 `7fca851` 未通过，指出五项缺陷（测试删除真实数据库、端到端未执行Alembic、临时目录残留风险、Windows盘符相对路径未处理、文档状态失实）。第四轮整改完成：测试重写为完全使用 `tmp_path` 并带真实数据目录守卫夹具，`alembic.ini` 改用 `%(here)s`，新增子进程真实Alembic升级与跨目录启动端到端测试，盘符相对路径明确拒绝，文档状态修正。全部验证在含哨兵真实数据库的干净临时副本中执行并通过，哨兵数据库字节级未变。

影响：R-101仍处于"整改完成、待主审复验"状态，T-102暂不开始。远程CI证据（复验项11）需项目负责人提供私有远程仓库后补齐。
