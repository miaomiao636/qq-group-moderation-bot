# 项目进度

## 当前阶段

**主通道架构调整为NapCat/OneBot（2026-09-08，决策D-019）**：项目负责人已实际确认，个人认证官方机器人无法开启“添加到任意群聊”，在数百/数千人目标群的邀请列表中不显示。因此QQ官方机器人降为可选/测试通道，NapCatQQ + OneBot 11改为生产大群主通道。代码基线 `2d8f405` 包含官方通道契约、审核、AI、动态规则、案件、报告、官方动作编排和R-104修复，**尚无NapCat入站与撤回/禁言/警告Adapter**。当前下一项只可开始T-305。

**R-104质量阻塞已关闭（2026-09-08）**：`app/reports/stats.py` 仅做格式化，提交 `2d8f405` 已推送；本地pytest为203 passed / 1 skipped，mypy 49个源文件通过，ruff check与format check通过。GitHub Actions运行 `34186194703` 的Ubuntu、Windows和干净运行时依赖三个任务全部成功。

**历史已完成基线**：

**R-103、T-105、T-204、T-205、T-106实现与远程CI已通过（2026-09-06，分支 `feature/r103-ai-rule-learning`）**：已修复R-103正确性问题，并实现后台版本化动态规则、远程AI软证据、管理员反馈候选规则学习和官方撤回/禁言/警告动作编排。本地全量门禁通过：pytest 203 passed / 1 skipped、mypy 48个源文件通过、ruff check/format通过；Alembic临时库完成 `upgrade head → current → downgrade base → upgrade head`；干净运行时依赖环境可导入应用和AI适配器，且不包含pytest。功能分支已推送，Pull Request #1 上分支 tip（`27fcf6d`）的最新 CI 运行 `34028677558` 与较早的 `34028509570`（@`d00960d`）均在 Ubuntu、Windows 与干净运行时依赖三个任务全部成功（详见 `HANDOFF.md`）；PR #1 已于 2026-09-06 合并（合并提交 `761fdba`），`main` 现含 R-103+T-105+T-204+T-205+T-106 全套实现。默认仍为 `ACTION_MODE=SHADOW`，真实QQ群自动处罚、真实MiMo调用、Windows 24×7和NapCat尚未实机验收。

## 已完成

- **R-103正确性整改 + T-105/T-204/T-205/T-106实现（2026-09-06，本分支）**：
  1. `ProcessedEvent` 改为带 `lease_token`、`lease_expires_at`、`attempts`、`next_retry_at`、`error_kind` 的租约领取模型；同一事件未过期 `PROCESSING` 不能被重复领取，永久契约失败只幂等记录一次并终止重试。
  2. 媒体存储改为完整SHA文件名、流式配额检查、`.part`计入、仅嗅探头部字节，避免配额越界、截尾碰撞和全文件读入。
  3. 文字规则加入语境校准：咨询、否定、反诈教育和引用语境不因单个“兼职/刷单”等词自动高风险；允许来源卡片可放行，未知来源只记录。
  4. 管理后台所有状态修改统一登录、CSRF和 `AdminAudit` 审计。
  5. 动态规则已落库：全局/群级规则集、不可变版本、草稿、发布、回滚、审计、5秒缓存热更新和判定版本追踪；候选规则只可复制为草稿，发布仍需人工。
  6. 远程AI以供应商无关接口接入：`TextModerator`/`VisionModerator`、OpenAI-compatible适配器、按群启用、脱敏上传、严格JSON、缓存、限流/预算和降级记录；单模型只作为软证据，不直接处罚。
  7. 反馈学习已接入后台：管理员可标注确认违规/确认正常/误判/未知原因等反馈，本地挖掘短语、域名、联系方式候选规则，未知撤回不当真值。
  8. 官方动作编排新增 `ACTION_MODE=SHADOW/OFFICIAL` 和 `EMERGENCY_STOP`：先落库动作意图和幂等键，再调用官方撤回、分级禁言和首次警告；数据库失败不调用外部动作，未知结果转人工，不盲目重放；任何路径都不产生踢人动作。
  9. 本地验证：`uv run pytest -q` 203 passed / 1 skipped；`uv run mypy app` 48文件通过；`uv run ruff check app tests alembic` 与 `uv run ruff format --check app tests alembic` 通过；临时SQLite库Alembic完整升降级通过；独立运行时虚拟环境 `uv sync --locked --no-dev` 可导入 `app.main` 和 `app.adapters.ai.openai_compatible`，且pytest不可导入。
  10. 远程验证：分支已推送至 `origin/feature/r103-ai-rule-learning`；Pull Request #1 的CI运行 `34028509570` 在head提交 `d00960d` 上完成，Ubuntu质量、Windows质量和干净运行时依赖三个任务全部成功；分支 tip 现推进至 `27fcf6d`，对应最新 CI 运行 `34028677558` 同样三个任务全部成功（链接见 `HANDOFF.md` 远程CI证据小节）。

- **R-102 核心正确性整改（已交付，2026-09-06）**：
  1. 流水线按图片/GIF/语音/视频/文件类型分发对应引擎（pipeline._is_* + media_engine），禁止全部进图片引擎；
  2. 去重重写为 begin_processing→mark_processed/mark_failed，失败可重试（processed_events 新增 status/error_message 列，迁移 d2b1f9a60e45）；
  3. 媒体缺失/下载失败/解析失败 → record_only，绝不 allow；
  4. 新增 app/adapters/qq_official/media.py：流式大小限制（图50MB/视频200MB）、安全文件名、磁盘配额2GB、媒体清理 purge_media 接入报告清理；
  5. GIF缓存键改为完整帧哈希集合 sha256，避免首帧相同误命中；
  6. TextRuleEngine.evaluate 使用 self._blacklist（含 extra_blacklist）；
  7. ReviewGate 重做：硬证据=主决策命中 R001/R003，软信号（R002）不算硬证据，不重复调用同规则；
  8. 案件审计 from 在赋值前捕获；案件幂等立案（同群同成员已有PENDING_REVIEW则复用）；case_no 冲突重试；
  9. 新增 tests/test_r102.py 10项回归（失败重试、语音/视频/PDF主链路、媒体缺失、GIF多帧缓存、extra_blacklist、复核门软信号拦截、审计from/to、并发幂等）。


- **T-301/T-302 管理后台与人工工作流（已交付）**（2026-09-05）：
  - `app/web/`：auth（会话登录，prod强制非空密码）、confirm（5分钟一次性确认码，用后即焚）、routes（案件列表/详情/证据页——成员标注"未验证QQ号"；审批流=预览+确认码→已人工踢出结案；保留/误判撤销/取消；规则视图；报告页+手动清理入口）。
  - 全部转换经 case_sm 状态机（双出口互斥、越权转换拒绝），operator 记入审计。
- **T-203 复核门与成本控制（已交付）**（提交d3b1655）：双通道一致性复核（仅黑名单/联系方式硬证据支持自动处罚，否则转人工）、CostLedger（零预算恒0）、CircuitBreaker（连续失败熔断+半开探测）。
- **T-401/T-402 报告与数据生命周期（核心已交付）**：build_daily/build_weekly/pending_manual_review；purge_expired（30天原始快照占位替换、180天动作日志删除，naive UTC比较修复）；报告页+手动清理入口；推送渠道与告知文案待负责人确认。

- **T-202 视频/语音/文件审核（已交付）**（2026-09-05，提交9a4cbf4）：
  - `app/moderation/media_engine.py`：语音优先用官方 `asr_refer_text` 转写复用文字规则（D-013）；视频本地 ffmpeg 抽帧≤16帧复用图片引擎，超200MB/10分钟只告警；文件只提取 PDF(pypdf)/纯文本，其他类型仅元数据记录，**绝不执行文件**。
  - 一切失败路径 record_only（ffmpeg缺失/解析失败/无转写）。12项新测试（含真实 ffmpeg 生成视频）。
- **T-201 图片/GIF审核引擎（已交付）**（2026-09-05，提交5183dec/399afcd）：
  - `app/moderation/imaging.py + image_engine.py`：dHash感知哈希（相似阈值10）、GIF抽帧≤8、二维码解码（zxing-cpp，小程序码不可解属预期）、黑/白名单哈希匹配、结果缓存；判定失败=record_only绝不处罚；聚合层 merge_decisions（媒体违规升级、媒体放行不降级文字违规）。
  - 新增依赖 pillow/zxing-cpp/pypdf。18项测试（合成图+真实样本存在时回归）。

- **T-104 违规阶梯与案件证据（已交付）**（2026-09-05，提交36c9479）：
  - `app/cases/`：models（violation_records/cases 表）、case_sm（案件状态机，双出口互斥、FAILED只转人工、终态不可回退）、service（30天窗口计数、第一次撤回+禁言1h+警告、第二次撤回+禁言24h+不警告+生成PENDING_REVIEW案件合并证据、误判撤销联动案件关闭）。
  - 决策结构永无kick：PlannedAction 仅允许 recall/mute/warn；保护角色拒绝进入阶梯。
  - 迁移 `e8f4a1b26c57`；17项新测试（状态机正常/非法/互斥/窗口/撤销）。
- **T-103 规则引擎与文字审核（已交付）**（2026-09-05，提交8155bdd/0dc1ca0/e9a12a4）：
  - `app/moderation/`：normalization（NFKC/零宽/谐音变体：裙→群、薯→书、抖y→抖音等）、extract（手机号/QQ号/微信号/URL/域名/支付宝口令，结果遮蔽）、decision（ModerationDecision，动作仅recall/mute/warn，无kick）、rules（黑名单0.70/弱信号递增0.30+0.20n/联系方式0.25起/变体+0.10；高置信阈值0.90；未达阈值record_only；保护角色只记录；刷屏1分钟>5条相同指纹=0.95确定性违规）。
  - 19项测试：5条真实违规文字全命中高置信、5条正常聊天全放行、离线评测报告可复现。

- **T-102 统一消息契约与官方适配器（已交付）**（2026-09-05）：
  - `app/adapters/qq_official/`：contract（StandardMessage 统一契约）、parser（12类真实样本全解析）、auth（令牌缓存+提前刷新）、actions（撤回/禁言/警告，超时10s、幂等重试、警告不重试、保护角色错误码标记）、dedup（`processed_events` 表+内存热路径）、audit（`action_logs` 审计落库）。
  - 新增迁移 `c7d2e8f91a03`（processed_events + action_logs）；新增运行时依赖 `tzdata`（Windows 时区）。
  - 62项测试全绿（含12份脱敏样本契约回归），mypy/ruff 全过。

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
- **W0 Windows基础兼容门禁（已通过）**（2026-09-04）：
  - 在Windows 10专业版测试机（Build 19045.6466，AMD64）上新克隆私有仓库（main @ `c22b0c1`）并执行完整W0门禁：`uv sync --all-groups`（46包，exit 0）、`uv run alembic upgrade head`（`init system_meta`，SQLite）、`uv run pytest` 24 passed、`uv run mypy app` 7文件无问题、`uv run ruff check` 与 `ruff format --check` 全通过。
  - 实际端口验证：`WEB_PORT=8135` 启动后 `/healthz` 在8135端口返回200；默认8000端口健康检查亦通过。
  - 配置拒绝验证：非法 `LOG_LEVEL=BOGUS`、越界 `WEB_PORT=99999`、Windows盘符相对路径 `DATABASE_URL` 三类非法配置均被拒绝启动，错误信息明确。
  - 系统记录（决策D-011）：Windows 10专业版 22H2 Build 19045.6466；CPU 12th Gen Intel i5-12400（AMD64）；内存15.7GB；磁盘C: 149.3GB（余79.4）/ D: 781.5GB（余727.8）/ E: 465.8GB；有线网卡Realtek Gaming 2.5GbE（链路1Gbps）；最新补丁KB5071982/KB5071959/KB5072653（安全更新，2026-07-18）。
  - 全程未配置任何QQ或模型密钥；详细证据见 `HANDOFF.md` 的W0小节。

## 进行中

- **T-001 QQ官方能力验证**：**进行中（核心能力已全部实测通过，2026-09-05）**。连通性、全量消息、撤回（含幂等）、禁言3600s/86400s（含解除）、保护角色拒绝均已实测；结论已写入决策D-012；9类脱敏样本入库。剩余：限流响应与媒体URL失效未实际触发、分享卡片事件形态待确认。
- **T-002 群规与样本准备**：**进行中（2026-09-05解除阻塞）**。群规边界初版+8张违规例图+5条违规文字已归档（规则基线 `docs/group-rules.md`，媒体存本机 `data/t002_media/` 不入库）；仍需万能校园墙允许海报归档、刷屏量化定义、色情/暴力类样本并补齐至验收数量。
- **T-404 Windows无人值守运行与故障恢复**：仅完成需求和验收标准，尚未实现或在Windows实机演练。
- **Windows正式测试环境**：W0基线已建立（2026-09-04）；24×7整机验收待T-404后进行。

## 已知问题

- **真实MiMo/其他远程AI待评测**：适配层已实现，真实模型ID、接口区域、计费、延迟、视觉能力和精确率/召回率必须用脱敏样本单独评测；模型结果当前只作为软证据。
- **T-105增强项未完成**：发布前差异预览、历史消息模拟回放、并发发布冲突提示仍需补强，当前基础后台可编辑/发布/回滚/热更新已经实现。
- **T-205增强项未完成**：候选规则目前主要覆盖短语、域名和联系方式；二维码、媒体哈希、来源和行为类候选、案件级批量标注与完整回放仍需后续增强。
- **T-106仅完成官方动作**：编排层已实现官方 `SHADOW/OFFICIAL`，但不能执行NapCat撤回/禁言/警告。必须先完成T-305中立动作契约和T-307 NapCat Adapter。
- **T-404仍未实现**：Windows Service、自启动、重启恢复、受控更新、健康检查和备份演练尚未完成；睡眠/休眠状态仍无法保证24×7实时处理。
- **NapCat主链路仍未接入**：尚无OneBot入站Adapter、中立身份契约、NapCat撤回/禁言/警告Adapter和Windows实机证据；分别由T-305、T-306、T-303和T-307完成。踢人仍必须人工批准，T-304是可选增强。
- **官方大群接入前提不成立**：个人认证账号无法开启任意群公开服务；T-001/D-012的小群能力实测结果保留，但不再作为目标大群上线证据。
- **当前 `main` CI失败**：运行 `34083954491` 在Ubuntu/Windows的格式检查阶段失败，未格式化文件是 `app/reports/stats.py`；R-104需修复并取得新的真实跨平台CI证据。

- **Windows CI 编码问题已修复**：Windows runner cp1252 编码读取含中文注释的 `alembic.ini` 会报 `UnicodeDecodeError`；现已 ASCII 化并在 CI 强制 `PYTHONUTF8=1`。
- **SQLite相对路径**：配置层规范化（`_normalize_sqlite_url`）与13项回归测试已就位；Windows盘符相对路径被明确拒绝；测试使用 `tmp_path` 并带真实数据目录守卫，不触碰真实数据库。
- **远程CI证据**：功能分支 `feature/r103-ai-rule-learning` 最新 CI 运行（tip `27fcf6d`，运行 `34028677558`）在 Ubuntu、Windows 与干净运行时依赖三个任务全部 `success`，运行链接见 `HANDOFF.md` 远程CI证据小节。
- 注意：`anyio.abc.BlockingPortal` 弃用警告来自 starlette 库，已在 pytest 配置中锁定。
- 注意：Windows 真实运行、自启动、重启和更新恢复需在 Windows 专用机通过 T-404 演练验证，当前 Mac 环境无法验证。
- QQ官方适配、审核、案件、后台和报告已有代码；NapCat主通道契约、入站、管理动作、无人值守和整体验收均未完成。

## 最近更新

日期：2026-09-08

修改内容：**完成R-104并推送NapCat主通道文档**。文档提交 `512daca` 与纯格式提交 `2d8f405` 已推送到远程 `main`。

验证：本地pytest 203 passed / 1 skipped，mypy 49个源文件通过，ruff check与format check通过；GitHub Actions运行 `34186194703` 的Ubuntu、Windows和干净运行时依赖任务全部成功。运行同时提示部分Action仍依赖已弃用的Node.js 20运行时，作为非阻塞维护项跟踪。

影响：R-104关闭，下一位Agent只可认领T-305；不得跳过传输中立契约直接接入NapCat动作。

### 历史更新

日期：2026-09-07

修改内容：**依据个人认证后台与目标大群实际结果，重新规划NapCat主通道**。负责人确认官方机器人只能在自建少人群显示，在只拥有管理员权限的数百/数千人目标群中不显示；个人主体不能开启“添加到任意群聊”。新增决策D-019，明确NapCat/OneBot为生产大群主通道，官方机器人保留为可选/测试Adapter；新增R-104、T-305、T-306、T-307并重写T-303/T-304/T-403/T-404依赖与Windows W1–W5门槛。

验证：本次只修改项目文档和任务计划，没有实现NapCat代码。重新核对Git后确认本地 `main` 与 `origin/main` 均为 `88ac433`；GitHub Actions运行 `34083954491` 为失败，Ubuntu/Windows都停在 `ruff format --check`，本地独立复现唯一未格式化文件为 `app/reports/stats.py`。

影响：下一位Agent不得继续按“官方机器人主通道”开发大群上线链路。必须先完成R-104，再按 `T-305 → T-306 → T-303 → T-307 → T-404 → T-403` 推进；T-304人工批准踢人可在T-307之后独立实现。

日期：2026-09-06

修改内容：**功能分支已推送并取得真实跨平台CI证据**。`feature/r103-ai-rule-learning` 已推送至私有远程仓库并建立 Pull Request #1；CI运行 `34028509570` 在提交 `d00960d` 上完成，Ubuntu质量、Windows质量和干净运行时依赖三个任务全部成功。PR尚未合并，`main`仍保持原状态。

验证：推送前重新执行 `uv run pytest -q`、`uv run mypy app`、`uv run ruff check app tests alembic` 和 `uv run ruff format --check app tests alembic`，全部通过；远程三项检查均为 `SUCCESS`。

影响：其他电脑和Agent现可获取功能分支；远程CI阻塞已关闭。下一步是审核并合并PR，之后按门槛进入W1/W2隔离群实测、真实MiMo脱敏评测和T-404实现。CI通过不等于真实QQ群处罚、Windows 24×7或NapCat整体验收通过。

日期：2026-09-06

修改内容：**分支 tip 最新跨平台CI实证刷新（只读API直接取证）**。功能分支 `feature/r103-ai-rule-learning` 当前 tip 提交 `27fcf6d` 的 CI 运行 `34028677558` 三个任务全部 `success`：Ubuntu 质量（`.../job/101474132870`）、Windows 质量（`.../job/101474132911`）、干净运行时依赖回归（`.../job/101474132949`）。较早的 `34028509570`（@`d00960d`）结论一致；两运行均属同一 Pull Request #1。

验证：通过 GitHub REST API（只读，Authorization: Bearer）直接读取 `actions/runs` 与 `actions/runs/{id}/jobs`，非本地推断；三个 job 的 conclusion 均为 `success`，无 `failure`/`cancelled`/超时。

影响：分支 tip 的远程CI阻塞确认关闭；PR #1 审核与合并是进入 W1/W2 隔离群实测前的唯一前置。真实自动处罚、真实MiMo和NapCat仍不因此次CI通过而开启。

日期：2026-09-06

修改内容：**Pull Request #1 已合并至 `main`**。经 GitHub API 合并（合并提交 `761fdba`），`main` 现包含 R-103、T-105、T-204、T-205、T-106 全部实现与文档更新；`feature/r103-ai-rule-learning` 的 tip（`a9a73fc`，含分支 tip CI 实证刷新）已合入。本地 `git fetch` 确认 `origin/main` 由 `ce2f2f7` 推进至 `761fdba`。

验证：PR 状态 `open`/`mergeable=true`，API `PUT /pulls/1/merge` 返回 `Pull Request successfully merged`，合并提交 `761fdba`；`git fetch origin` 显示 `ce2f2f7..761fdba main -> origin/main`。

影响：远程CI阻塞与合并阻塞均已关闭；下一步按W1/W2门槛进入隔离群动作实测（保持 `ACTION_MODE=SHADOW` 起步）。真实自动处罚、真实MiMo和NapCat仍不得因合并而开启。

### 此前更新

日期：2026-09-06

修改内容：**R-103正确性整改与AI/动态规则/反馈学习/官方动作编排实现完成本地验证**。分支 `feature/r103-ai-rule-learning` 新增并提交：事件租约领取、永久失败幂等、媒体存储加固、规则语境校准、后台CSRF/审计、动态规则数据库版本、OpenAI-compatible远程AI软证据、管理员反馈候选规则、官方动作意图与 `SHADOW/OFFICIAL` 编排。同步更新 `.env.example`、`README.md`、`PROJECT_CONTEXT.md`、`NEXT_TASKS.md`、`MEMORY_INDEX.md`、`AGENTS.md` 和交接记录。

验证：本地 `uv run pytest -q` 为203 passed / 1 skipped；`uv run mypy app` 48个源文件通过；`uv run ruff check app tests alembic` 与 `uv run ruff format --check app tests alembic` 通过；临时SQLite库Alembic完成 `upgrade head → current → downgrade base → upgrade head`；独立运行时虚拟环境 `uv sync --locked --no-dev` 可导入应用和AI适配器，且pytest不可导入。

影响：R-103旧阻塞已关闭到本地自动化层面；当时下一步为推送并取得Linux/Windows CI证据。真实自动处罚、真实MiMo和NapCat均不得因本地实现完成而直接开启。

### 更早更新

日期：2026-09-05（第四轮）

修改内容：**T-002 核心规则全部澄清完成**。负责人通过图片展示页逐张判定8张样本：编号1-4（无校园墙码）=违规，编号5-8（带校园墙码）=允许。最终白名单规则确立：**图片下半部带「万能校园墙」小程序码=允许来源（即使内容是兼职招聘）；无码才进入内容审核**；兜底：明显违法内容仍进人工复核。技术实现路径：该码为标准二维码，解码载荷字符串作为白名单标识键比对（无需解析小程序内容）。另确认：刷屏=1分钟>5条且相同字样/图片/表情包；色情/暴力类样本不全网搜集入库，改由内容安全服务商评测集承担（待负责人确认该验收口径调整）。manifest标签已更新，临时看图服务已关闭（8321端口仅本机临时使用）。

影响：T-102 适配器与 T-103 规则引擎的设计输入（消息契约、白名单规则、刷屏参数、边界原则）已全部就绪；T-002 进入持续补样本阶段（不再阻塞开发）。

### 此前更新

日期：2026-09-05（第三轮）

修改内容：**媒体通道实测打通 + T-002 解除阻塞**。①实测附件下载：8张图片经官方附件URL实时下载归档成功（`data/t002_media/`，gitignored）；②发现**媒体URL含rkey签名有时效**，过期返回400（约数小时前URL全部失效），机器人必须即时下载——写入决策D-013；③**分享卡片事件形态确认**：以`[卡片消息] 小程序`文本形态到达，含source/摘要/source_logo——T-001形态空白全部补齐；④语音附件含`asr_refer_text`（官方转写）与`voice_wav_url`字段，语音审核可优先用官方转写（D-013）；⑤负责人提供群规与样本：8张违规例图（兼职/刷单/引流，含谐音变体）+5条违规文字+允许规则（万能校园墙小程序海报白名单、群主/管理员白名单），规则基线入库 `docs/group-rules.md`，卡片与文字垃圾脱敏样本3份入库（fixture累计12份）。原始媒体与敏感联系方式不入仓库。

影响：T-001 可验证项全部完成（仅剩限流格式一项低风险）；T-002 由阻塞转为进行中（仍需允许海报归档、刷屏量化定义、色情/暴力类样本并补齐数量）；T-102 与 T-103 规则设计输入均已就绪。

### 此前更新

日期：2026-09-05（第二轮）

修改内容：**T-001 核心能力全部实测通过**。负责人将机器人设为群管理员并提供普通成员测试号后：①撤回普通成员消息实测成功（`DELETE /v2/groups/{group_openid}/messages/{message_id}` 返回200）；②**重复撤回同一消息返回200——撤回接口幂等**；③禁言3600秒与86400秒实测成功（`mute_expire_at` 到期时间制），`op=del` 解除禁言成功；④负面用例：尝试禁言群主被平台拒绝（HTTP 400 `40103004「目标成员为机器人/群主/管理员，不允许被禁言」`），保护角色为平台硬限制；⑤采集并脱敏入库5类新样本：GIF（image/gif+faceType=6）、语音（voice/amr）、视频（video/mp4）、文件（file/pdf）、转发记录（`[群聊的聊天记录]`文本骨架），样本总量9份；⑥确认附件含可下载 `url`（含width/height），媒体审核链路可行。全部结论已写入决策 **D-012**。

影响：T-001 仅剩限流响应、媒体URL失效、分享卡片形态三个低风险验证项（转入T-102/W1）；**T-102 适配器开发可立即开始**（依赖的官方能力均已实测确认）。

### 此前更新

日期：2026-09-05

修改内容：**T-001 QQ官方能力验证第一阶段完成**。负责人提供机器人应用（AppID 1905561634）与4个测试群，机器人已入群。实测结论：①`api.bot.qq.com` 令牌签发成功（注意：旧域名 `api.sgroup.qq.com` + `QQey` 前缀返回401，新文档统一为 `api.bot.qq.com` + `QQBot` 前缀）；②`/users/@me` 与 `/gateway` 通过，机器人身份与 WSS 网关确认；③**群聊全量消息事件实测生效**：WebSocket intents=1<<25 收到4条 `GROUP_MESSAGE_CREATE` 事件（含不带@的普通文字），覆盖文字@/普通文字/表情/图片（含元数据）四类；④事件含 `group_id` 字段但为32位不透明十六进制串而非真实数字群号，**OpenID↔数字QQ映射风险结论维持不变**；⑤撤回实测返回 `40062003 无操作权限`（机器人 member_role=member，需群管理员身份，且不能撤群主消息）；⑥禁言接口定义确认（POST `/v2/groups/{group_openid}/restrict_chat_setting`，members 数组、op=add/update/del、`mute_expire_at` RFC3339、单批≤20人、最长30天、不能禁言群主/管理员/机器人）。4类脱敏样本入库 `tests/fixtures/qq_official/`。凭据仅存本机 `.env`（已 gitignore），未入仓库。

影响：T-001 由阻塞转为进行中；剩余项：撤回/禁言实测（待机器人管理员身份+普通成员测试号）、GIF/语音/视频/文档/转发/卡片样本、限流与失败响应记录、结论写入 `DECISIONS.md`。T-102 可开始消息契约与适配器设计（事件结构已实测确认）。

### 此前更新

日期：2026-09-04

修改内容：**W0门禁独立复验通过（接手Agent）**。按 `AGENTS.md` "先检查实际运行结果再相信文档"的要求，接手Agent未直接采信文档记录，在同一台Windows 10专业版测试机（Build 19045.6466，AMD64，补丁KB5071982/KB5071959/KB5072653，2026-07-18）实际重跑全部W0门禁并全部通过：`uv sync --all-groups --reinstall` 锁文件级干净重装（46包，exit 0）、Alembic完整"降级base→升级head"周期（head `3a9c0c662c2e`）、pytest **24 passed**（24.59s）、mypy 7文件无问题、ruff check 与 format 检查通过、`WEB_PORT=8135` 与默认8000端口 `/healthz` 均返回200（`{"status":"ok","env":"local","mode":"SAFE"}`）。全程未配置任何QQ或模型密钥。本轮仅状态与证据记录，未修改代码与测试。

影响：W0门禁经独立复验确认，"已通过"结论与Windows基线可信；T-001与T-002仍阻塞，等待QQ官方应用/隔离群/权限与群规/白名单/脱敏样本；T-102依赖T-001。

### 此前更新

日期：2026-09-04

修改内容：**W0 Windows基础兼容门禁实机通过**。在Windows 10专业版测试机（Build 19045.6466，AMD64，i5-12400）上新克隆私有仓库（main @ `c22b0c1`），完成干净安装（`uv sync --all-groups`，46包）、Alembic迁移（`init system_meta`）、24项pytest、mypy、ruff check与format检查；`WEB_PORT=8135`实际生效；非法日志级别、越界端口、盘符相对路径数据库均被拒绝启动；健康检查在默认8000与自定义8135端口均返回200。按决策D-011记录build号、CPU架构、补丁状态、内存、磁盘与网络方式；全程未配置任何QQ或模型密钥。同步更新 `PROGRESS.md`、`NEXT_TASKS.md`、`HANDOFF.md`、`MEMORY_INDEX.md`、`PROJECT_CONTEXT.md`（仅状态记录，未修改代码）。

影响：W0由“待执行”转为“已通过”，Windows基线建立；T-001与T-002仍阻塞，等待QQ官方应用/隔离群/权限与群规/白名单/脱敏样本；T-102依赖T-001。

日期：2026-09-04

修改内容：**Windows测试机就绪确认（决策D-011）**。项目负责人确认正式测试机为Windows 10专业版；该机已安装Node.js LTS、Git、Python 3.12、FFmpeg与uv（由该机CodeBuddy执行）。`PROJECT_CONTEXT.md`、`docs/windows-operations.md`、`AGENTS.md`、`DECISIONS.md`中“优先Windows 11 x64”表述统一更正为Windows 10专业版。W0门禁尚未执行，Windows基线未建立。

影响：W0由“阻塞”转为“测试机已就绪、待执行门禁”；T-001与T-002仍阻塞，等待QQ官方应用/隔离群/权限与群规/白名单/脱敏样本。

日期：2026-09-04

修改内容：**R-101已由主审复验通过**。四轮整改（测试安全、真实Alembic端到端、盘符相对路径、文档状态、Windows CI编码修复）与远程CI取证（复验项11）全部完成并获主审确认。文档状态统一更新为"R-101已通过"。

影响：下一步为 W0（Windows基础兼容）、T-001（QQ官方能力验证）与 T-002（群规与样本准备）。T-102依赖T-001，须在T-001完成后才可开始。
