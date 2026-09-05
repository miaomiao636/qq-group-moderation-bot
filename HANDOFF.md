# Agent交接记录

## 日期

2026-09-04

## 当前任务

**R-102 核心正确性整改已交付（2026-09-06，基于远程 main de523dd）**。10项整改全部完成并补回归测试，全量门禁通过。**未启用真实撤回/禁言/警告/NapCat**；未推进 T-404/正式处罚/NapCat，已推送远程等待主审核。

## 已完成内容

### R-102 核心正确性整改（2026-09-06，接手Agent）

按主审10项要求执行，详见 `PROGRESS.md`。要点：
- 流水线按媒体类型分发（图片/GIF→image_engine、语音→evaluate_voice、视频→evaluate_video、文件→evaluate_file），不再全部进图片引擎；
- 去重：begin_processing 标记 PROCESSING，成功 mark_processed，失败 mark_failed 可重试（迁移 d2b1f9a60e45 加 status/error_message）；
- 媒体缺失/下载失败/解析失败 → record_only，绝不 allow（解析失败也落库一条记录）；
- 新增 app/adapters/qq_official/media.py：流式大小限制、安全文件名、磁盘配额2GB、purge_media 接入报告清理；
- GIF缓存键改完整帧 sha256；TextRuleEngine.evaluate 用 self._blacklist；ReviewGate 重做（硬证据 R001/R003，软信号 R002 不算硬证据，不重复调用）；
- 案件审计 from 赋值前捕获；案件幂等立案 + case_no 冲突重试；
- tests/test_r102.py 10项回归。全量 154+ 项测试、mypy 41文件、ruff 全过。

**CI 实证（GitHub Actions，提交 `9c27f44`+`1e15031`，运行 `33979817730`，结论 success）**：
- 运行总览：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33979817730
- Linux 任务：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33979817730/job/101342914510
- Windows 任务：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33979817730/job/101342914699
- 运行时依赖回归（clean install）：https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/33979817730/job/101342914707
- 注：首版 `9c27f44` 因测试实例写 `data/media/`（CI 无此目录）导致 pytest 失败；`1e15031` 改用 `tmp_path`+monkeypatch 修复，复跑全绿。

### 影子模式上线（2026-09-05第六批，接手Agent）

- **T-403影子接入**（提交8b709c6）：`app/runtime/` runner（WS常驻/心跳/退避重连/媒体即时下载）+ pipeline（解析→去重→文字规则→复核门→媒体→落库）+ shadow_decisions表（迁移b12f6d84aa77）+ 后台影子页。141项测试全绿。
- **D-015决策**（提交5e695a7）：报告渠道=仅管理后台网页；D-014零预算方案正式认可；隐私告知=起草→负责人审定（已完成）→群内公示→正式模式。
- 运行器以独立后台进程启动（日志 `$env:TEMP\shadow_out.log`/`shadow_err.log`）；注意：该进程不受Windows Service监督（T-404范围），机器重启需手动重跑或完成T-404服务化。

### P3/P4 交付（2026-09-05第五批，接手Agent）

## 已完成内容

### P3/P4 交付（2026-09-05第五批，接手Agent）

- **T-301/T-302 管理后台与人工工作流**：`app/web/`（auth会话登录、confirm 5分钟一次性确认码、routes 案件列表/详情/证据/审批/保留/误判/取消/规则视图/报告页+清理入口）。审批流=预览（成员标注"未验证QQ号"）→生成确认码→凭码确认；全部转换经状态机（双出口互斥）。11项Web集成测试。
- **T-203 复核门与成本控制**：双通道一致性复核（无独立硬证据即拦截转人工）、CostLedger（零费用台账）、CircuitBreaker。9项测试。
- **T-401/T-402**：build_daily/build_weekly/pending_manual_review 报告构建器；purge_expired 数据保留清理（30天快照占位替换/180天日志删除）；报告页+手动清理入口。报告推送渠道与隐私告知文案待负责人确认。

### P1 审核链路交付（2026-09-05，接手Agent，负责人授权连续推进）

## 已完成内容

### P1 审核链路交付（2026-09-05，接手Agent，负责人授权连续推进）

- **T-102**（提交141f8c3）：`app/adapters/qq_official/` 六模块（contract/parser/auth/actions/dedup/audit）+ 迁移c7d2e8f91a03 + tzdata依赖；12份脱敏样本契约回归全过；动作适配器带超时/有限重试/警告不重试/保护角色错误码标记。
- **T-103**（提交8155bdd/0dc1ca0/e9a12a4）：`app/moderation/` 四模块；置信度模型可解释（黑名单0.70、弱信号0.30+0.20n、联系方式0.25起、变体+0.10、刷屏0.95）；阈值0.90；真实样本5正例全命中5反例全放行；决策结构无kick。
- **T-104**（提交36c9479）：`app/cases/` 三模块 + 迁移e8f4a1b26c57；30天窗口、两次阶梯（1h/24h禁言、第二次不警告并立案合并证据）、误判撤销联动闭案、案件状态机双出口互斥（FAILED只转人工、终态不可回退）。
- 质量门禁：91项pytest、mypy 24文件、ruff check/format 全部通过（Windows实机）。

## 已完成内容

### T-002 首批材料归档 + 媒体通道实测（2026-09-05第三轮，接手Agent）

- 负责人提供群规：允许「万能校园墙」小程序海报与正常聊天；禁止广告/色情/黄色/暴力/血腥/恐怖；不可刷屏；群主与管理员为人员白名单。
- 规则基线结构化入库 `docs/group-rules.md`（脱敏：第三方手机号/群号不写入仓库）；含对规则引擎的直接影响：二维码必须结合白名单匹配（允许海报的码与其他引流码并存）、变体规避（"薯/5一直要"、"裙"代"群"）需归一化。
- 媒体实测：升级监听器支持附件下载；8张违规例图经官方URL实时下载归档 `data/t002_media/`（gitignored，含manifest清单）；**发现URL rkey签名有时效（过期HTTP 400），必须即时下载**——决策D-013。
- 新形态确认：分享卡片以 `[卡片消息] 小程序` 文本到达（含source/摘要/source_logo）；语音附件含 `asr_refer_text` 官方转写与 `voice_wav_url`——均写入D-013。
- 新增脱敏样本3份（share_card/text_spam×2），fixture累计12份。
- 执行备注：首轮下载脚本文件名冲突导致覆盖，已用事件内URL补下载恢复全部8张（无需用户重发）。

### T-001 QQ官方能力验证·核心实测（2026-09-05第二轮，接手Agent）

- 前置就位：负责人已将机器人设为测试群管理员，并提供普通成员测试号（member_role=member）。
- 撤回实测：`DELETE /v2/groups/{group_openid}/messages/{message_id}` 对普通成员消息返回 HTTP 200；**对同一消息重复撤回再次返回200——接口幂等**，动作层重试可直接依赖。
- 禁言实测：`POST /v2/groups/{group_openid}/restrict_chat_setting`，3600秒与86400秒（`mute_expire_at` RFC3339到期时间）均返回200；`op=del`+空到期时间解除禁言返回200。
- 保护角色负面用例：尝试禁言群主返回 HTTP 400 `40103004「目标成员为机器人/群主/管理员，不允许被禁言」`——平台层硬限制；业务层仍需自行拦截白名单普通成员（平台不保护）。
- 媒体样本采集（监听窗口实测收到）：GIF（image/gif，content含faceType=6标记）、语音（voice，.amr）、视频（video/mp4）、文件（file，.pdf）、转发记录（content为`[群聊的聊天记录]`文本骨架，630字符）。附件结构含 `url`（可下载）与 width/height。
- 脱敏入库：`tests/fixtures/qq_official/` 新增5份（gif/voice/video/forward_record/file_pdf），累计9份；文件名遮蔽保留扩展名、转发记录逐行脱敏、附件URL遮蔽。
- 结论归档：全部实测结论写入 `DECISIONS.md` 决策 **D-012**。
- 执行方式备注：监听/撤回/禁言均为系统临时目录一次性验证脚本（未入仓库），实测后目标测试号的禁言已解除、消息已撤回，未在测试群遗留副作用。

### T-001 QQ官方能力验证·第一阶段（2026-09-05，接手Agent）

- 外部资源到位：负责人创建机器人应用（AppID 1905561634，机器人UIN 4017145414）与4个隔离测试群，机器人已入群并被授权查看消息；凭据由负责人提供后仅写入本机 `.env`（已在 `.gitignore`），未入仓库与文档。
- 连通性：`api.bot.qq.com/app/getAppAccessToken` 签发成功。**排障记录**：先用旧域名 `api.sgroup.qq.com` + `Authorization: QQey` 前缀，`/users/@me` 与 `/gateway` 均返回401（空错误体）；改用现行文档统一域名 `api.bot.qq.com` + `QQBot` 前缀后全部通过。后续适配器必须使用新域名与新前缀。
- 身份与网关：`GET /users/@me` 200（机器人ID/头像/share_url 正常）；`GET /gateway` 200，返回 `wss://api.sgroup.qq.com/websocket`。
- 全量消息事件：临时 WebSocket 监听器（intents=1<<25，系统临时目录脚本，未入仓库）收到4条 `GROUP_MESSAGE_CREATE` 事件，其中包含**不带@的普通文字消息**，证明全量消息事件已生效；覆盖文字@（`<@member_openid>` 标记+mentions）、普通文字、表情（`<faceType=...>`）、图片（attachments 含 content_type/size 元数据，无content）。
- 关键发现：事件中的 `group_id` 字段是32位不透明十六进制串，**不是真实数字群号**；作者字段含 `member_role`（实测群主为 `owner`、机器人自身在mentions中为 `member`）。**OpenID↔数字QQ映射风险结论维持不变**，人工客户端/NapCat回退设计不变。
- 撤回实测：`DELETE /v2/groups/{group_openid}/messages/{message_id}` 返回 HTTP 400 `{"code":40062003,"message":"无操作权限"}`——机器人当前 member_role=member；且被撤回对象为群主消息（群主消息可能不可撤）。需群主将机器人设为群管理员，并准备普通成员测试号后复测。
- 禁言接口定义确认（与"mute_seconds"旧假设不同）：`POST /v2/groups/{group_openid}/restrict_chat_setting`，请求体 `members` 数组（单批≤20），元素含 `op`（add/update/del）、`member_openid`、`mute_expire_at`（RFC3339到期时间）；最长30天；**不能禁言群主/管理员/机器人本身**；解除禁言用 `op=del` + 空 `mute_expire_at`。1小时禁言=当前时间+3600s 的 RFC3339 时间戳。尚未实测。
- 脱敏样本：4份入库 `tests/fixtures/qq_official/`（text_at/text_plain/face/image），ID遮蔽保留长度与前后缀、消息ID保留 `ROBOT1.0_` 前缀、用户名与正文替换为占位符、message_scene.ext 令牌遮蔽、机器人自身昵称与成员角色保留；经抽查无真实内容泄漏。

### W0独立复验（接手Agent，2026-09-04，本机=正式Windows 10专业版测试机）

按AGENTS.md"先检查实际文件、运行结果和测试状态，再相信文档"的要求，本轮接手Agent未直接采信前一轮W0文档记录，而是在同一台Windows 10专业版测试机上实际重跑全部W0门禁，结果全部通过：

- 系统信息复核（与决策D-011记录一致）：Windows 10专业版 22H2，Build **19045.6466**（CurrentBuild 19045 / UBR 6466，BuildLabEx 19041.1.amd64fre）；架构 **AMD64**；最新补丁KB5071982/KB5071959/KB5072653（安全更新，2026-07-18）、KB5066130/KB5066790（2026-07-16）。
- 干净安装：`uv sync --all-groups --reinstall` 强制按锁文件重装全部包（Resolved 48 / Prepared & Installed 46，exit 0），等效于全新环境安装。
- 迁移：`alembic current`（head）→ `downgrade base` → `upgrade head` → `current` 完整迁移周期通过，head版本 `3a9c0c662c2e`（init system_meta）。
- 质量门禁：`uv run pytest` **24 passed**（24.59s）；`uv run mypy app` Success: no issues found in 7 source files；`uv run ruff check app tests alembic` All checks passed!（exit 0）；`uv run ruff format --check app tests alembic` 13 files already formatted（exit 0）。
- 实际端口：`WEB_PORT=8135` 启动后 `GET /healthz` 在8135端口返回 HTTP 200，body `{"status":"ok","env":"local","mode":"SAFE"}`；默认端口8000启动后同样返回 HTTP 200。
- 密钥边界：全程未创建 `.env`，未配置任何QQ AppID/AppSecret、NapCat地址或模型API Key，符合W0定义。
- 执行环境备注：①本轮因审批超时未手工删除 `.venv`/`data/`，改用 `--reinstall` 实现锁文件级干净重装、以"降级到base再升级到head"完整周期替代全新库迁移，验证力度等价；②ruff 运行时出现 `.ruff_cache` 写入 `拒绝访问 (os error 5)` 警告但检查结果与退出码不受影响，属执行环境权限特性，非代码问题。

### W0 Windows基础兼容门禁（2026-09-04，实机通过）

- 测试机系统记录（按决策D-011）：Windows 10专业版 22H2，Build **19045.6466**；CPU 12th Gen Intel(R) Core(TM) i5-12400，架构 **AMD64**；内存15.7GB；磁盘C: 149.3GB（余79.4）/ D: 781.5GB（余727.8）/ E: 465.8GB；网络为有线以太网，Realtek Gaming 2.5GbE网卡（链路1Gbps）；最新补丁KB5071982/KB5071959/KB5072653（安全更新，2026-07-18）、KB5066130（更新）与KB5066790（安全更新，2026-07-16）；系统安装于2026-07-12，最近启动2026-07-19。
- 代码获取：该机通过Git Credential Manager凭据克隆私有仓库 `miaomiao636/qq-group-moderation-bot`（main @ `c22b0c1`，工作区干净），克隆过程未要求交互认证。
- 干净安装：`uv sync --all-groups` 创建 `.venv`（CPython 3.12.10），Resolved 48 / Installed 46 个包，exit 0。
- 迁移：`uv run alembic upgrade head` 成功执行 `Running upgrade -> 3a9c0c662c2e, init system_meta`（SQLite），exit 0。
- 质量门禁：`uv run pytest` **24 passed**（25.28s）；`uv run mypy app` Success: no issues found in 7 source files；`uv run ruff check app tests alembic` All checks passed；`uv run ruff format --check app tests alembic` 13 files already formatted。
- 实际端口：`WEB_PORT=8135` 启动后 `/healthz` 在8135端口返回200，uvicorn日志确认监听 `http://127.0.0.1:8135`。
- 配置拒绝：非法 `LOG_LEVEL=BOGUS`、越界 `WEB_PORT=99999`、Windows盘符相对路径 `DATABASE_URL=sqlite+aiosqlite:///C:relative\blocked.db` 三类非法配置均被拒绝启动（ValidationError，错误信息明确可操作）。
- 健康检查：默认8000与自定义8135端口均返回 `{"status":"ok","env":"local","mode":"SAFE"}`。
- 约束遵守：全程未配置任何QQ AppID/AppSecret、NapCat地址、模型API Key；`.env` 未创建，应用以SQLite默认配置（`APP_ENV=local`、`RUN_MODE=SAFE`）启动。
- 执行备注：该机非交互PowerShell以GBK编码解析命令，中文字面量路径会被误读导致解析失败；本次全部改用相对路径与通配符解析规避，属执行环境特性，不影响项目代码。
- 遗留提示：本轮W0在该机新克隆仓库执行（机器非完全空白，已预装开发工具与Node.js/FFmpeg等多媒体组件），符合PROJECT_CONTEXT中"R-101后先验证基础安装和Windows兼容性"的阶段定义；本机文档变更尚未提交git。

### Windows 10专业版测试机就绪确认（决策D-011，2026-09-04）

- 项目负责人确认正式整机测试机为Windows 10专业版；该机已通过CodeBuddy安装Node.js LTS、Git、Python 3.12、FFmpeg与uv。
- 新增决策D-011：替代D-010中“优先Windows 11 x64”的初始假设；代码保持跨平台通用，不做Windows专用分支。
- 同步更正 `PROJECT_CONTEXT.md`、`docs/windows-operations.md`、`AGENTS.md`、`NEXT_TASKS.md`、`PROGRESS.md`、`MEMORY_INDEX.md` 中“优先Windows 11 x64”表述。
- W0状态由“阻塞（无电脑）”转为“测试机已就绪、门禁待执行”；门禁真实通过前不得宣称W0完成。
- 本轮仅为文档状态更新，未修改任何代码；无测试可执行，验证方式为文档一致性核查。

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

- 本轮（T-002归档+媒体实测）修改：`DECISIONS.md`（新增D-013）、`NEXT_TASKS.md`、`PROGRESS.md`、`HANDOFF.md`、`MEMORY_INDEX.md`；新增 `docs/group-rules.md`、`tests/fixtures/qq_official/` 3份样本（累计12份）；本机归档 `data/t002_media/`（8张违规例图+manifest，gitignored）。未修改任何应用代码。
- 前轮（T-001第一阶段）修改：`NEXT_TASKS.md`、`PROGRESS.md`、`HANDOFF.md`；新增 `tests/fixtures/qq_official/`（4份脱敏样本）。未修改任何应用代码。
- 前轮（W0交接）修改：`PROGRESS.md`、`NEXT_TASKS.md`、`HANDOFF.md`、`MEMORY_INDEX.md`、`PROJECT_CONTEXT.md`（仅状态与证据记录，未修改任何代码与测试）。
- 新增：`app/__main__.py`、`tests/test_sqlite_path.py`。
- 修改：`pyproject.toml`、`uv.lock`、`app/config.py`、`app/db.py`、`app/main.py`、`tests/conftest.py`、`tests/test_health.py`、`alembic/env.py`、`alembic/script.py.mako`、`alembic/versions/3a9c0c662c2e_init_system_meta.py`、`alembic.ini`、`.github/workflows/ci.yml`、`.env.example`、`.gitignore`、`README.md`、`AGENTS.md`、`DECISIONS.md`、`PROGRESS.md`、`NEXT_TASKS.md`、`HANDOFF.md`、`docs/windows-operations.md`。

## 验证结果

### W0 实机验证证据（2026-09-04，执行机=正式Windows 10专业版测试机）

- `uv sync --all-groups`：exit 0，46包安装（运行时+开发组，CPython 3.12.10）。
- `uv run alembic upgrade head`：exit 0，`Running upgrade -> 3a9c0c662c2e, init system_meta`（SQLite）。
- `uv run pytest`：24 passed in 25.28s。
- `uv run mypy app`：Success: no issues found in 7 source files。
- `uv run ruff check app tests alembic`：All checks passed!（exit 0）。
- `uv run ruff format --check app tests alembic`：13 files already formatted（exit 0）。
- 实际端口：`WEB_PORT=8135` 下 `GET http://localhost:8135/healthz` → HTTP 200，body `{"status":"ok","env":"local","mode":"SAFE"}`；日志确认监听 `http://127.0.0.1:8135`。
- 配置拒绝：`LOG_LEVEL=BOGUS`、`WEB_PORT=99999`、`DATABASE_URL=sqlite+aiosqlite:///C:relative\blocked.db` 三类非法配置进程均快速退出并抛出 ValidationError（分别为非法日志级别可选值提示、端口上限65535校验、盘符相对路径明确拒绝并给出修正建议）。
- 默认端口健康检查：`GET http://localhost:8000/healthz` → HTTP 200。
- 密钥边界：未创建 `.env`，未配置任何QQ或模型密钥，符合W0定义。
- 干净安装说明：测试机为新克隆仓库 + 全新 `.venv` 安装；系统级工具（Node/Git/Python/uv/FFmpeg）此前已装（决策D-011），与W0门禁无关，W0以门禁命令真实通过为准。

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

- **T-002 待补（持续，不阻塞开发）**：各类别样本距离验收数量（≥20正例+30反例，媒体各≥10）差距大，负责人持续提供。
- **决策D-014已定**：不采购内容安全服务商（负责人零预算确认）；色情/暴力类=平台风控兜底+文字规则+可选本地开源分类器+人工复核；该类别召回率不承诺；日报需含"仅记录待人工"列表。
- **T-001 剩余项（低风险）**：限流触发阈值与限流响应格式未实测（不主动刷限流）；群主消息撤回行为未测（非计划依赖能力）。
- **凭据安全提示**：AppSecret 曾出现在聊天记录中，建议 T-001 收尾后由负责人在开放平台重置一次，并改由负责人直接维护 `.env`。
- **凭据安全提示**：AppSecret 曾出现在聊天记录中，建议 T-001 收尾后由负责人在开放平台重置一次，并改由负责人直接维护 `.env`。
- **R-101已通过**：四轮整改与远程CI取证（复验项11）均已完成并获主审确认，无遗留阻塞项。
- **W0 已通过（2026-09-04）且经接手Agent同日独立复验通过**：Windows基线已建立，两轮证据见"验证结果"W0小节。遗留提示：首轮在该机新克隆仓库执行（机器非完全空白，已装开发工具），符合"R-101后先验证基础安装和Windows兼容性"阶段定义；文档变更已随本轮提交入库。
- **T-001 阻塞**：需要项目负责人提供QQ官方应用、隔离测试群与全量消息/撤回/禁言权限。
- **T-002 阻塞**：需要项目负责人提供群规、白名单与脱敏样本。
- Windows 真实运行、自启、重启和更新恢复需在 Windows 专用机通过 T-404 演练验证，当前 Mac 环境无法验证。
- T-404 只有文档和验收标准，尚无实际实现与实机证据。

## 下一步建议

1. （已完成，2026-09-04）W0门禁已在Windows 10专业版测试机实机通过；接手Agent同日独立重跑全部门禁亦全部通过（见"W0独立复验"小节），本轮文档变更已提交入库。
2. T-001所需机器人应用/隔离群已就位；请项目负责人：①在测试群将机器人设为群管理员；②准备普通成员测试号发消息供撤回/禁言实测；③继续提供 T-002 所需群规/白名单/脱敏样本。事件结构已实测确认，T-102 的消息契约与适配器设计可并行启动。
3. 完整Windows阶段和门槛见 `docs/windows-operations.md`；W1（QQ官方链路）在T-001与T-102通过后开始。

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
