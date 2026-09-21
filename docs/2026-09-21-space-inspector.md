# 电脑 QQ 空间限制巡检

任务：`SPACE-INSPECT-20260921`。负责人已接受首版只识别 QQ 空间明确限制提示，未命中保留待确认；正式使用不连接手机。此为明确追加的新功能，不是重开主审已关闭问题。

## 使用

桌面入口为 **QQ空间限制巡检**。代码在本仓库 `app/space_inspector/`，独立运行，不依赖 Codex，不需要启动另一个后台服务。

1. 打开工具，刷新群列表。群成员来源是项目已配置的机器人账号；QQ 空间观察账号由专用浏览器正常登录，两者分别显示。
2. 点击“打开空间登录”，在独立 Microsoft Edge 中登录 QQ 空间，然后点击“确认已登录”。首次登录和之后的会话过期需要本人处理；不复制现有浏览器或 QQ 的凭据。
3. 选择需要检查的群，点击“开始检查所选群”。每个任务最多选择 10 个群；同一账号跨群只访问一次，导出保留每个群的关联。
4. 可以暂停、关闭后载入任务继续。点击“载入已有任务”，在窗口内按创建时间、群名和进度选择任务，再点“载入所选”；默认选择最新任务，不必寻找隐藏的 AppData 目录。“其他位置”保留手动选择入口。载入不会自动开始巡检；点“继续检查”才续扫。任务逐项保存；同一任务必须使用原成员来源账号和空间观察账号才能续扫。仅查看或导出已有任务不需要重新登录空间。需要查看文件时点“打开任务目录”；未载入任务时打开任务根目录。AppData 默认隐藏，也可按 Win+R 输入 `%LOCALAPPDATA%\QQSpaceInspector\tasks` 直接进入。
5. 点击“导出结果”，再点“打开导出目录”。`restricted.csv` 仅包含明确空间违规限制提示；`report.csv` 包含全部快照成员；`report.json` 保存观察依据、统计和群快照信息；`说明.txt` 解释范围与未完成数量。CSV 可用 Excel 打开。

不要在扫描期间手动切换专用浏览器的页面或账号。已核验的“主人设置了权限”“对方未开通空间”页面记为待确认并继续；遇到登录失效、访问失败或其他未知页面会保存进度并暂停，提示具体 QQ 号及原因，用户处理后才能继续。未完成任务也可导出，导出不会把未完成成员当作正常。

## 判据与边界

- 唯一阳性判据是已核验的 QQ 空间顶层系统错误区域，明确显示“您访问的空间存在违规信息,已被多名用户举报,暂时无法查看！”。同时核对目标 URL、完整加载状态和当前查看账号。
- 不根据昵称、头像、等级、在线状态或上传名单推断异常；用户内容里的相同文字也不能命中。
- “未观察到该提示”不代表账号正常；空间限制也不等于 QQ 账号永久失效。首版不是完整 QQ 异常账号检测器。
- 成员关联依据为本机接口返回后保存的快照，不是扫描结束时服务器成员关系的证明。接口声明人数、快照人数分别保存，可能不一致；`saved_at` 是快照本地保存时间，`checked_at` 是页面检查时间。
- 访问逐个进行，检查之间保留固定间隔。不会绕过验证码、风控或访问限制；不存在一条已经验证的 QQ 官方“全群异常账号查询”接口。
- 不连接处罚编排，不踢人、不改群审核/真实动作开关、不启用 enforce、不迁移或重启生产服务。手机辅助原型与历史证据保留，但不作为本版日常使用前提。

## 安装与本地数据

要求 Windows、带 Tk 的 Python 3.12+、uv、Microsoft Edge，以及项目已有的本机 NapCat HTTP 目录接口。运行：

```powershell
./scripts/install-space-inspector.ps1 -PythonPath '<Python 3.12 python.exe>'
```

安装脚本使用 `uv sync --locked --no-dev --extra inspection`，在 `%LOCALAPPDATA%/QQSpaceInspector/runtime` 安装独立环境并创建桌面快捷方式。Playwright 是可选依赖，不加入生产基础依赖；脚本不修改主服务配置。

数据目录：`%LOCALAPPDATA%/QQSpaceInspector/`。`browser/` 是工具自己的浏览器资料目录，含正常登录会话；`tasks/` 是巡检任务与导出，均仅保存在本机，不提交 Git。不要共享整个目录。群目录默认读取 `D:/QQ/config/onebot11_<ONEBOT_SELF_ID>.json`；其他部署可用进程环境变量 `QQ_SPACE_NAPCAT_CONFIG_DIR` 指定目录，不在界面输入或展示令牌。

目录适配器只调用 `get_login_info`、`get_group_list`、`get_group_member_list`，固定账号、回环地址、无代理、无重定向；读取群/成员前后核对登录身份。专用浏览器和任务分别加操作系统锁，异常退出不留下永久逻辑锁。只保存最小页面观察，不保存整页资料或登录令牌。

## 代码与内部回归来源

- 依赖提交：`7c3a9796ae210a597825dd043106fb72637214cf`，仅 `pyproject.toml`、`uv.lock`。
- 代码提交：`67ea1c4439abebfb8a577958e5c1f98f530e0e43`，仅 `app/space_inspector/` 与安装脚本。
- 内部回归提交：`98ddc527279183cd92819b5970ad22d7d53d5ab5`，仅 `tests/test_space_inspector_*.py`。

内部回归为本次自建测试，不冒充外部主审探针。子 Agent 仅在 TEMP 草拟，root 独立复核后复制/修正并入库，仓库保持单一写入者。临时验证不作为 CI 入库证明。

整改包含：对异常页面保存完整依据后才记完成、限流和总截止、同群冲突拒绝、查看账号切换即停、跨群去重、快照封存、未完成任务恢复、导出防公式注入、逐项持久化、关闭失败可重试且不提前释放锁。新增回归先复现失败，再验证修复；未修改主审探针。

## 验证证据

**实页研究与产品验收分开。** 实页研究基线为 `37ee80b2bdc2970cfc6a104aa8298a5e3876c601`；使用已正常登录的 Codex 浏览器，实际工具命令为 `qzoneTab.goto(...)`、`getAXState()`、`playwright.evaluate(...)`。这不是当时已完成的独立桌面工具测试。

私有观察存于 `C:/Users/81596/AppData/Local/QQAccountInspector/evidence/20260921T115916Z-qzone-validation/`，为工具结构化输出的忠实转录，不是原始 HTTP 响应或服务器签名。按 QQ 去重、采用最后观察，用户标注的 10 个样本中 9 个显示该限制提示，1 个未显示；无正常成员真值对照，不将此比例称为总体准确率或覆盖率。复算命令：

```powershell
uv run python 'C:/Users/81596/AppData/Local/QQAccountInspector/evidence/20260921T115916Z-qzone-validation/summarize.py'
```

复算脚本执行 SHA：`98ddc527279183cd92819b5970ad22d7d53d5ab5`；原观察执行基线仍为上述 `37ee80b2...`。`observations.json` SHA256：`bc6a6abdf661aa0b1f48b0febbeaa86e70480ac64535533375591ad6dbfaaee5`；`collector.js` SHA256：`fe5b54b9b73d56e3c1ed60c24b93a90db14faaf5b60ccd69370d087184db9720`。账号原件留在本机，不提交仓库。

本机门禁执行 SHA：`98ddc527279183cd92819b5970ad22d7d53d5ab5`：

```powershell
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
uv run pytest --junitxml='C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/verification-98ddc52/full.xml'
```

上述 SHA 的全量已完成：2134 项，2118 passed、16 skipped、0 failed/error；其中主审探针 31 文件/262 项全部通过，巡检内部回归 137 项通过。三条门禁均通过：格式检查 343 文件，mypy 110 源文件。这是权限页修复前的完整结果，不能替代下方补丁版本验证。证据在上述 `verification-98ddc52/` 的 `gates.json`、各检查输出、`full.log/xml`。

桌面工具已创建并启动；负责人已反馈专用浏览器登录后工具显示账号。该项为用户实机反馈。群扫描、暂停、续扫、导出与最终 CI 的状态将在本记录追加，不把用户登录反馈写成全链验收。

## 实机反馈后的权限页修复

负责人在首个成员处反馈暂停。执行 SHA `98ddc527279183cd92819b5970ad22d7d53d5ab5` 的工具记录为 `BLOCKED / unrecognized_page`；正常登录和群快照已成功，但扫描未能继续。核对用户截图及 `qzoneTab.goto(...)` + `playwright.evaluate(...)` 实页 DOM，页面实际是“主人设置了权限，您可通过以下方式访问”。它不是违规限制提示。

修复提交 `3290f27783178af3237fcf1f868b12fb6cee2e3d`，仅识别核验过的顶层权限模板与“申请访问”控件，记 `UNCONFIRMED / space_access_permission_required` 并继续；不点击申请访问。未知模板和安全验证仍暂停。回归提交 `376f17773d8227cc09581737e6c81365737aad5a`，保留此前测试并补权限页存储恢复验证。已有任务的 BLOCKED 记录可继续，历史不覆盖。

补丁全量执行 SHA `376f17773d8227cc09581737e6c81365737aad5a`，命令与上方一致，但 JUnit/日志目录换为 `verification-private-page-fix/`。该执行 SHA 全量 2139 项：2123 passed、16 skipped、0 failed/error；其中主审探针 31 文件/262 项全部通过，巡检内部回归 142 项全部通过。三条门禁通过（343 格式文件、110 类型源文件）。未跳过新增巡检回归，也未修改主审测试。

本轮桌面安装使用 `scripts/install-space-inspector.ps1`，只写用户目录与桌面快捷方式，已验证独立环境导入。修复后的程序需要退出旧窗口再打开以载入新代码；登录会话与原任务保存。最终 HEAD 的 CI 必须单独核对，不借用旧 run。

## 第二种权限页与历史任务入口

负责人重新创建任务后仍在后续成员处暂停。截图与实页 DOM 表明是“加为好友后访问”布局：申请链接位于 `.add_friend_access.access_option`，此前仅查找 `.apply_access`。代码提交 `37957de85ee01144bfb6a4fbdb641f53b95cc45f` 改为已观察到的共同容器 `.access_option`，仍要求精确权限提示、可见“申请访问”、正确目标与查看账号和完整加载，不修改阳性判据、不点击加好友或申请访问。

同提交增加最近任务选择窗口，只读读取本工具任务摘要并保留 WAL 中的最新进度；载入、继续检查分开。历史读取失败仍保留“其他位置”入口。回归提交 `aa03122f5d445b5940cfac4281ce979f0f88ed4c` 覆盖真实临时 SQLite、WAL、外来/损坏结构、读取边界、只读约束及 worker 不自动开始扫描。内部测试与外部主审探针分开，后者未修改。

在执行 SHA `aa03122f5d445b5940cfac4281ce979f0f88ed4c` 上完成以下隔离验证（TEMP 脚本未入库，不能称为 CI 回归）：

```powershell
& 'C:/Users/81596/AppData/Local/QQSpaceInspector/runtime/Scripts/python.exe' 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/verify_permission_dom.py'
& 'C:/Users/81596/AppData/Local/QQSpaceInspector/runtime/Scripts/python.exe' 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/verify_history_ui.py'
```

前者以全请求拦截的合成 HTML 在新建无登录浏览器中运行生产 DOM 提取器和分类器：7 个场景通过，包含两种权限布局、未知权限文字、错误申请控件、明确限制提示、普通资料页和用户内容中的相同文字。后者不构造 Service、不接网，实际构造 Tk 组件并读取本机任务摘要：找到 2 个历史任务，最新任务显示已检查 5/993、限制提示 1；默认选最新、载入只提交 resume、错误时保留其他位置、关闭时不弹窗均通过。组件验证不能替代负责人重开新版本后的实机续扫。

同期只读核对负责人实际任务，执行 SHA 同上，命令：

```powershell
uv run python 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/inspect_saved_task.py' 'C:/Users/81596/AppData/Local/QQSpaceInspector/tasks/20260921T124031Z-621478da' --output 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/history-fix-live-read-20260921.json'
```

该只读快照总成员 993，已检查 5（观察到限制 1、待确认 4），未完成 988（含当前受阻成员）。这是修复前保存的用户实机结果，不是新修复已完成续扫的证明；读取脚本的执行 SHA 也不能证明旧 GUI 进程已加载同一版本。

另在 `814ddda394d4c0c2274e69f22e3690d4b0fc165f` 的 Store/导出代码上，对该任务的一致性副本做组件导出核验；当时 browser.py 有权限容器修改，但 Store/导出未修改。副本位于本机 `evidence/partial-desktop-export-copy-20260921/`，未写原任务。以下复算通过，全部 CSV 与 JSON 成员对应，完整报告 993 行、限制报告 1 行；仍仅检查 5 人，不能写成全群完成或用户已完成 GUI 导出：

```powershell
uv run python 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/verify_export.py' 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/partial-desktop-export-copy-20260921/exports/20260921T125053Z-1f4d2cb1cd9c' --source-sha 814ddda394d4c0c2274e69f22e3690d4b0fc165f
```

## 第二次补丁的全量与门禁

执行 SHA：`aa03122f5d445b5940cfac4281ce979f0f88ed4c`；验证期间仅追加交接文档，源码和测试未变。命令：

```powershell
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
uv run pytest --junitxml='C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/verification-history-fix/full.xml'
uv run python 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/summarize_history_verification.py'
```

全量 2156 项：2139 passed、17 skipped、0 failed/error；主审探针 31 文件/262 项全通过；巡检内部回归 159 项中 158 passed、1 skipped。相比此前新增的 skip 是本机没有创建 symlink 权限的真实链接测试；模拟 Windows reparse 拒绝测试仍通过，不削弱原断言。三条门禁通过：格式 345 文件、类型 111 源文件。原始证据为上述 `verification-history-fix/` 中的 `full.log/xml`、`summary.json`、`gates.json` 与各门禁输出。

此前文档 HEAD `814ddda394d4c0c2274e69f22e3690d4b0fc165f` 的 [CI run 35601171462](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35601171462) 已三 job 成功，核验命令 `gh run view 35601171462 --json headSha,status,conclusion,jobs`。这条旧 run 不覆盖本次补丁。推送后的最终 CI 应按实际 headSha 核对，不借用旧结果：

```powershell
git rev-parse HEAD
gh run list --branch windows-deploy-2026-09-10 --limit 5 --json databaseId,headSha,status,conclusion,url
gh run view <匹配最终HEAD的run_id> --json headSha,status,conclusion,jobs
```

## 未开通空间页导致的后续暂停

负责人再次反馈暂停，原任务已越过之前的权限页。执行 SHA `64f77a78522281987022f90ff657363cd79ec0f5` 只读核验命令如下：

```powershell
uv run python 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/inspect_saved_task.py' 'C:/Users/81596/AppData/Local/QQSpaceInspector/tasks/20260921T124031Z-621478da' --output 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/next-pause-live-read-20260921.json'
```

保存的实机进度为 993 名成员、已检查 78（限制提示 5、待确认 73）、未完成 915（含当前受阻成员）。当前受阻原因为 `unrecognized_page`。随后在同一查看账号下，用 `qzoneTab.goto(任务记录中的成员页面)`、`getAXState()`、`playwright.evaluate(...)` 核对，实际顶层错误区域是“对方未开通空间”，附“邀请开通 / 返回我的空间”，没有违规报告图标；不是登录失效或已观察到违规提示。私有最小 DOM 转录保存在 `evidence/unopened-page-dom-20260921.json`，不含凭据，也不是原始 HTTP 响应。

内部新增回归在基线 `64f77a78522281987022f90ff657363cd79ec0f5`（仅测试未提交修改）执行 `uv run pytest tests/test_space_inspector_browser.py tests/test_space_inspector_store.py -k unopened -q -o addopts= --tb=short`，真实复现 2 failed、5 passed：未开通模板被判 BLOCKED，存储拒绝新的依据来源。

代码提交 `f74b8ed3b30de9fa9065565696b416133cf60d04`：严格匹配已核验的顶层未开通模板，记 `UNCONFIRMED / space_not_opened`，绑定目标、查看账号和完整加载状态不变；存储允许该最小依据并保留原暂停历史。另加“打开任务目录”按钮、暂停时显示具体 QQ 与原因。未改阳性判据、未知页暂停策略或生产模块。回归提交 `5f54f877d04e20b7f99a83ae49a77dbc54b5484c`，覆盖未知模板仍停、恢复保留历史、导出依据以及实际调度继续下一成员。

在执行 SHA `5f54f877d04e20b7f99a83ae49a77dbc54b5484c` 执行以下 TEMP 验证通过：生产分类器回放实页最小转录得到待确认；任务目录回调通过正确路径与打开失败提示的隔离检查。操作系统打开动作已用测试替身替代，不宣称实际操作了用户资源管理器；没有推进真实任务。这项未入库，不当作 CI 测试：

```powershell
& 'C:/Users/81596/AppData/Local/QQSpaceInspector/runtime/Scripts/python.exe' 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/verify_unopened_page.py'
```

### 未开通页面补丁验收

执行 SHA `5f54f877d04e20b7f99a83ae49a77dbc54b5484c`（期间只追加文档，源码/测试未变）：

```powershell
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
uv run pytest --junitxml='C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/verification-unopened-fix/full.xml'
uv run python 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/summarize_unopened_verification.py'
```

全量 2164 项：2147 passed、17 skipped、0 failed/error。主审 31 文件/262 项全部通过；巡检内部回归 167 项中 166 passed、1 skipped（Windows symlink 权限，原因与上一轮相同）。格式 345 文件、类型 111 源文件，门禁全部通过。证据在 `verification-unopened-fix/` 的 JUnit、日志、摘要及门禁输出。

负责人反馈已能继续。上述 SHA 下只读执行：

```powershell
uv run python 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/inspect_saved_task.py' 'C:/Users/81596/AppData/Local/QQSpaceInspector/tasks/20260921T124031Z-621478da' --output 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/unopened-fix-live-resume-20260921.json'
```

保存的真实记录确认原阻断成员已以 `space_not_opened / UNCONFIRMED` 完成，旧 BLOCKED 历史仍保留，任务继续检查后续成员。该快照已检查 105/993，观察到限制 8、待确认 97、未完成 888，无当前 BLOCKED 成员。这是一次有界续扫成功，不代表整群完成，也不将未开通空间算成异常账号。

同时核对用户通过 GUI 生成的已有导出（导出生成于本补丁前），执行 SHA 同上：

```powershell
uv run python 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/verify_export.py' 'C:/Users/81596/AppData/Local/QQSpaceInspector/tasks/20260921T124031Z-621478da/exports/20260921T133058Z-9b51c9c5dd37' --source-sha 5f54f877d04e20b7f99a83ae49a77dbc54b5484c
```

CSV/JSON 一致性通过：该旧导出包含快照成员 993 行，导出时已检查 78、限制 5、待确认 73、未完成 915。此为用户 GUI 导出的核验，不再只是复制任务的组件导出；仍不能代替本补丁续扫后的新导出或全群验收。最终文档 HEAD 的 CI 仍按上方查询命令逐项核对，旧成功 run 不作为本补丁证明。

## 待验证

权限页及未开通页面修复后的有界实机续扫均有保存记录证明，已有 GUI 导出也已核对；未开通页面补丁后的新增实机结果导出尚待核验。全群覆盖、其他页面、长期运行及 QQ 页面升级兼容性仍需实际使用验证。方案 A 主审裁定与 Windows 回滚维护窗口仍是独立待办，不因本功能完成而关闭。
