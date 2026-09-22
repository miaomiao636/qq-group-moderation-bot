# 电脑 QQ 空间限制巡检

任务：`SPACE-INSPECT-20260921`。负责人已接受首版只识别 QQ 空间明确限制提示，未命中保留待确认；正式使用不连接手机。此为明确追加的新功能，不是重开主审已关闭问题。

**当前可用性结论：有界续扫和部分结果导出已有实机证据，但整群连续扫描遭腾讯 WAF 拦截，整群一次扫完验收未通过。当前不能承诺为稳定的全群批量巡检工具。平台拦截必须停止，不作为成员异常依据；后续续扫已有新增保存进度，但触发规则、持续恢复及低频分批整体可行性均未验证。**

## 使用

桌面入口为 **QQ空间限制巡检**。代码在本仓库 `app/space_inspector/`，独立运行，不依赖 Codex，不需要启动另一个后台服务。

1. 打开工具，刷新群列表。群成员来源是项目已配置的机器人账号；QQ 空间观察账号由专用浏览器正常登录，两者分别显示。
2. 点击“打开空间登录”，在独立 Microsoft Edge 中登录 QQ 空间，然后点击“确认已登录”。首次登录和之后的会话过期需要本人处理；不复制现有浏览器或 QQ 的凭据。
3. 正常访问恢复后，选择需要检查的群，点击“开始检查所选群”。本次优化默认开启自动续批：每批 300 人、相邻实时检查等待 30 秒、批间休息 60 秒，按保存的待检查队列继续。窗口可调间隔、批量及休息，关闭自动续批则一批结束后停止；这些均为试验设置，不是腾讯允许频率，仍可能被拦截。每个任务最多选择 10 个群；同一账号跨群只访问一次，导出保留每个群的关联。
4. 可以暂停、关闭后载入任务继续。点击“载入已有任务”，在窗口内按创建时间、群名和进度选择任务，再点“载入所选”；默认选择最新任务，不必寻找隐藏的 AppData 目录。“其他位置”保留手动选择入口。载入不会自动开始巡检；点“继续检查”才续扫。任务逐项保存；同一任务必须使用原成员来源账号和空间观察账号才能续扫。仅查看或导出已有任务不需要重新登录空间。需要查看文件时点“打开任务目录”；未载入任务时打开任务根目录。AppData 默认隐藏，也可按 Win+R 输入 `%LOCALAPPDATA%\QQSpaceInspector\tasks` 直接进入。
5. 点击“导出结果”，再点“打开导出目录”。`restricted.csv` 仅包含明确空间违规限制提示；`report.csv` 包含全部快照成员；`report.json` 保存观察依据、统计和群快照信息；`说明.txt` 解释范围与未完成数量。CSV 可用 Excel 打开。历史复用行额外标注观察来源、原观察时间、复用时间和来源任务，不表示本次重新访问。

不要在扫描期间手动切换专用浏览器的页面或账号。已核验的“主人设置了权限”“对方未开通空间”和“功能升级维护，暂不支持非好友访问”页面记为待确认并继续；遇到登录失效、访问失败或其他未知页面会保存进度并暂停，提示具体 QQ 号及原因，用户处理后才能继续。未完成任务也可导出，导出不会把未完成成员当作正常。

遇到腾讯安全防护拦截时，工具显示平台拦截原因，并撤销本次浏览器确认状态、禁用继续扫描，仍允许导出。先停止重试；待本人确认正常访问已经恢复后才重新确认浏览器。程序不设置声称有效的自动冷却时间，不轮换账号/IP，不跳过拦截继续访问下一成员。

## 判据与边界

- 唯一阳性判据是已核验的 QQ 空间顶层系统错误区域，精确匹配已核验的两套完整提示：英文冒号“温馨提示:”配句末“！”，或全角冒号“温馨提示：”配句末“。”；正文均为“您访问的空间存在违规信息,已被多名用户举报,暂时无法查看”加对应标点，末段均为“返回我的空间”。同时核对目标 URL、完整加载状态和当前查看账号。
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

## 整群尝试遭平台拦截

负责人尝试一次检查完整群，反馈 `https://waf.tencent.com/501page.html` 页面显示“您的访问被拦截”。此为平台安全防护拦截，不是被检查成员的空间违规提示。腾讯官方 [WAF 使用说明](https://cloud.tencent.com/document/product/627/59443) 描述了此类拦截页面和请求标识；该文档不能证明本次命中何种规则，也没有给出本任务的恢复时长。连续访问可能是诱因，不能据此断言命中固定频率阈值、账号封禁或指定等待时长即可恢复。

执行 SHA `2076e402612ed2acefa1135c458d6aed7fa55f16` 的只读核验：

```powershell
uv run python 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/inspect_saved_task.py' 'C:/Users/81596/AppData/Local/QQSpaceInspector/tasks/20260921T124031Z-621478da' --output 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/waf-stop-live-read-20260921.json'
```

当时保存进度为已检查 213/993，限制提示 18、待确认 195、未完成 780（含受阻成员）。旧版本把这次跳转记作 `BLOCKED / unexpected_location`，没有把受阻成员计作限制或完成。负责人截图提供 WAF 页面证据；原历史记录保留，不事后改写成新版本观察。此次整群验收结论是未完成、受平台拦截，不能以已命中部分样本替代整群可行性。

新增内部回归在基线 `2076e402612ed2acefa1135c458d6aed7fa55f16`（仅新增测试未提交）执行 `uv run pytest tests/test_space_inspector_browser.py -k waf -q -o addopts= --tb=short`，复现 2 failed：WAF 被泛化成地址不一致；浏览器确认错误引导重新登录。

代码提交 `614081eb87a643c85b506bc30a705ac2d7d7be13`，回归提交 `23bcb33015781ee533bfb7f88daa824ef5992a63`。仅识别已见的精确 HTTPS WAF 域名和路径，保存 `BLOCKED / platform_access_blocked`、`platform_access_block` 来源；不保存跳转查询参数/请求标识，不归因成员异常。整个任务暂停，worker 向界面发出重新确认浏览器的状态，继续检查禁用、导出保留。浏览器仍停在 WAF 时仅检查当前页面，不导航重试。未绕过平台保护、未自动重试、未更改请求间隔或业务判定。

执行 SHA `23bcb33015781ee533bfb7f88daa824ef5992a63` 的独立 Tk 状态验证通过，命令如下。worker 和系统交互已替换为隔离测试，不访问 QQ，不算平台恢复验证，也不是入库 CI 测试：

```powershell
& 'C:/Users/81596/AppData/Local/QQSpaceInspector/runtime/Scripts/python.exe' 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/verify_platform_pause_ui.py'
```

## 待验证

当前优先保留并导出已有结果，停止连续访问；负责人已选正常访问恢复后低频分批，尚未验证恢复或真实新批次。低频分批仅是尚未验证的负载控制实验，不能承诺避开拦截或完成全群。已有有界续扫及 GUI 导出证据不变；新增结果导出、整群覆盖、其他页面、长期运行及 QQ 页面升级兼容性仍需实际验证。方案 A 主审裁定与 Windows 回滚维护窗口仍是独立待办。


## 低频分批与网上替代路线研究

负责人已选恢复正常访问后再试低频分批，并要求检索 GitHub 替代路线。代码 `ac80853`、回归 `3b2f42e67a7b280bc18c60b9748cad09240d3719`：桌面 scan 显式传入每批最多 10 次检查、批内等待 30 秒；不会自动开启下一批，暂停能中断等待，批末无需额外等待。WAF 停止及重新确认、保存/导出保持。未访问被拦截的 QQ 页面，未验证平台恢复；此参数只用于有界实验，不能承诺全群可用。

内部先在 `23bcb33015781ee533bfb7f88daa824ef5992a63` 加未提交回归执行 `uv run pytest tests/test_space_inspector_service.py -k 'batch_limit or low_frequency' -q -o addopts= --tb=short`，5 failed，分别复现缺少批次上限和桌面未显式传入低频参数。测试原样保留并新增等待/暂停覆盖。

网上研究为只读源码审查，无第三方程序安装或执行，无登录凭据共享。GitHub `gh api repos/<owner>/<repo>/commits/HEAD --jq .sha` 核对研究版本；`gh api repos/<owner>/<repo>/contents/<path>` 解码读取下列源码。检索 `gh search code '"空间存在违规信息"' --limit 10 --json repository,path,url` 和 `gh search code '"暂不支持查看资料卡"' --limit 10 --json repository,path,url` 未返回条目；这仅是本次检索结果，不证明所有公开/私有实现均不存在。

- [NapCat GetUserStatus](https://github.com/NapNeko/NapCatQQ/blob/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-onebot/action/extends/GetUserStatus.ts)：源码明确为在线状态，不是账号冻结或空间违规查询，不能据此替代阳性判据。
- [onebot-qzone 错误类型](https://github.com/Gu-Heping/onebot-qzone/blob/2d016d2b60130923e060f8bc469d271d397879e0/src/qzone/infra/errors.ts) 和 [响应解析](https://github.com/Gu-Heping/onebot-qzone/blob/2d016d2b60130923e060f8bc469d271d397879e0/src/qzone/requestLayer.ts)：包含限频、鉴权及反爬错误，反爬明确不可重试。它可作接口结构研究参考，但已审查部分没有提供可直接用于本任务的成员失效分类器；换库不能据此保证消除拦截。
- [qzone-sdk](https://github.com/Eganchiyu/qzone-sdk/tree/2282b8bc50c185eb6f8f7a39a3c8c6a517484d77)：README 主要是空间内容操作；`src/qzone_sdk/utils/response.py` 仅统一成功/错误返回，不证明能够识别成员失效。
- [QQ 空间导出助手作者 FAQ](https://github.lvshuncai.com/archives/qzone-export-issue.html)：属于内容备份，作者区分访问权限与被封空间且不保证完整导出；不能把可备份内容当成账号正常或可靠检测接口。

后续接口研究须先用明确空间限制、权限页、未开通和普通资料页对照验证返回语义，再评估有界请求下的可用性；候选库文档或非零错误码不直接作为成员异常证据。当前没有已验证的替换接口，也未将任何候选接入正式工具。


### 本轮冻结版本验证

执行 SHA `3b2f42e67a7b280bc18c60b9748cad09240d3719`，验证期间仅更新文档，源码/测试未变：

```powershell
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
uv run pytest --junitxml='C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/verification-low-frequency/full.xml'
uv run python 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/summarize_low_frequency_verification.py'
& 'C:/Users/81596/AppData/Local/QQSpaceInspector/runtime/Scripts/python.exe' 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/verify_low_frequency_ui.py'
```

全量 2179 项：2162 passed、17 skipped、0 failed/error；主审 31 文件/262 项全部通过；巡检内部回归 182 项中 181 passed、1 skipped（同前 Windows symlink 权限）。格式 345 文件、类型 111 源文件，三门禁通过。`verification-low-frequency/` 保留 full.log/xml、summary.json、gates.json 与门禁输出。独立 Tk 验证通过批末手动继续提示、WAF 禁用继续和保留导出；该私有脚本不是入库 CI 用例，不访问 QQ、不替代实机验收。

最终文档 HEAD 推送后，按前述 `gh run list` / `gh run view` 命令核对同一 SHA 的三个 job；最终 CI 结果见对应 GitHub run 与交付回执。旧 `2076e402612ed2acefa1135c458d6aed7fa55f16` 的 run `35607666860` 三 job 已成功（`gh run view 35607666860 --json headSha,status,conclusion,jobs`），只证明旧版本，不能代替本轮 CI。


## 第二种限制提示模板

负责人反馈某成员页已显示空间违规提示，工具却暂停为 `unrecognized_page`。本轮通过 Codex 浏览器 `reviewTab.goto(...)`、`getAXState()`、`playwright.evaluate(...)` 核对实页：相同顶层系统区域 `.page > .page_main > .error_content`、单个 `.report_img`、正确目标和查看账号；段落为全角冒号“温馨提示：”、正文句末“。”和“返回我的空间”。旧版本仅接受英文冒号搭配句末“！”，因此漏识别。本次暂停不是批次上限，也没有观察到 WAF 页面。

代码提交 `d9ac14b021e94d2f2b54aa6d552206ad3acadebc`；内部回归提交 `94427140777b76e245f6f400a193de1c35830cef`。共享契约维护两套已观察的完整模板，浏览器分类与持久化使用同一来源；保留原文标点，不做模糊匹配或任意标点归一化。错误身份、页面未完成、正文混入用户内容和未知模板仍阻断，WAF 停止与批次参数未改；无生产服务重启、业务判定调整或数据库迁移。

先在 `d2a4f6b22b7b1629a4a08b64f7b355d604e5a06d` 加未提交内部测试执行：

```powershell
uv run pytest tests/test_space_inspector_browser.py tests/test_space_inspector_store.py -k 'period or fullwidth' -q -o addopts= --tb=short
```

结果 2 failed、8 passed、55 deselected：分别复现分类器漏识别与存储拒绝新原文。新增测试已原样入库，保留此前标点严格匹配测试及所有主审探针；覆盖模板成对匹配、身份/完整性校验、续扫保留旧访问历史、重开任务和导出保留原文。

执行 SHA `94427140777b76e245f6f400a193de1c35830cef`：对上述真实页面的最小 DOM 工具输出做忠实转录，调用生产分类器得到 `RESTRICTION_OBSERVED`。转录保存在本机 `QQSpaceInspector/evidence/period-template-dom-20260921.json`，不是原始 HTTP，也不是专用桌面 GUI 已重开的证据：

```powershell
uv run python 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/verify_period_template.py'
uv run python 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/inspect_saved_task.py' 'C:/Users/81596/AppData/Local/QQSpaceInspector/tasks/20260921T124031Z-621478da' --output 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/period-template-live-read-20260921.json'
```

同 SHA 的只读任务快照：993 人、已检查 281、观察到限制 24、待确认 257、未完成 712（含当前受阻 1）；是负责人旧窗口已保存结果，不是新补丁续扫证明。原任务未改写，BLOCKED 仍待重查，成功后追加访问记录而保留旧历史。独立运行环境已确认导入本仓库新代码；旧窗口需要关闭后从桌面重开，通过窗口内“载入已有任务”选择原群后继续，不要求新建任务或寻找隐藏目录。本补丁 GUI 续扫与新增结果导出仍待实机核对。


### 补丁后的实机续扫与冻结验证

负责人重开窗口后反馈“已越过，继续检查了”。同执行 SHA `94427140777b76e245f6f400a193de1c35830cef` 只读核对保存结果：

```powershell
uv run python 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/inspect_saved_task.py' 'C:/Users/81596/AppData/Local/QQSpaceInspector/tasks/20260921T124031Z-621478da' --output 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/period-template-resumed-read-20260921.json'
```

快照时间 `2026-09-21T15:57:14Z`：已检查 284/993，限制提示 25、待确认 259、未完成 709、当前受阻 0。原暂停成员现新增 `RESTRICTION_OBSERVED` 记录，依据保留句号版本正文；之前 4 条 `BLOCKED / unrecognized_page` 历史仍在。结合用户反馈与落盘记录，可确认本次实机续扫越过该模板，不能据此推断全群完成、低频参数安全或新版 GUI 导出已验收。

冻结源码/测试执行 SHA 同上，命令：

```powershell
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
uv run pytest --junitxml='C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/verification-period-template/full.xml'
uv run python 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260921/summarize_period_verification.py'
```

全量 2189 项：2172 passed、17 skipped、0 failed/error；主审 31 文件/262 项全通过；巡检内部回归 192 项中 191 passed、1 skipped（原有 Windows symlink 权限差异）。格式 345 文件、类型 111 源文件，三门禁通过。证据目录 `verification-period-template/` 保留 full.log/xml、summary.json、gates.json 和三门禁输出。代码、内部回归分别提交，主审探针未改；最终文档 HEAD 的 CI 另按 `gh run list --branch windows-deploy-2026-09-10 --limit 5 --json databaseId,headSha,status,conclusion,url`、`gh run view <run-id> --json headSha,status,conclusion,jobs` 查询，最终回执不借用旧 SHA。


## 每批上限调整为 300 人

负责人明确要求把一次检查上限改为 300 人试用，并确认已暂停另一个任务的仓库修改，由本任务接手单一写入。对齐基线 `adef6ee558be3ba9d53de7111c6eecfd21a74ef9`，工作区干净。代码提交 `e4d582e7d10f3e2f4c0122eb14102a8fed042c15` 仅把 `EXPERIMENT_BATCH_SIZE` 从 10 改为 300；内部回归提交 `2623031f7dabdb65703bc047507478ceda302517` 更新用户已变更的参数预期，并覆盖批次边界和剩余成员续扫。未修改外部主审探针或降低暂停/身份/存储保护。

GUI 说明直接引用同一常量；批内间隔仍为 30 秒，每次点击开始或继续最多访问 300 个待检查成员，批末手动续批。遇平台拦截、登录失效或未知页面仍提前停止；本次不安排无人值守连批、不自动开始真实扫描。满批仅间隔时间为 `(300 - 1) * 30 = 8970` 秒，约 2.5 小时，另加网页加载时间；不能把增大批次当作已经解决访问限制。

为避免与另一任务同时写仓库，先将候选内部回归放入 TEMP，在上述基线运行：

```powershell
uv run pytest 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260922-batch300/test_space_inspector_service.py' -k 'desktop_scan_uses or desktop_batch_300' -q -o addopts= --tb=short
```

结果 2 failed、14 deselected，复现旧桌面仍传入 10 人、到第 10 人就停止。取得写入交接后候选入库；最终回归证明模拟 301 人任务首批恰好访问 300 人、第二批只访问剩余 1 人，不重复已完成成员。该合成回归不接 QQ，不能当作 300 人实机成功证据。

执行 SHA `2623031f7dabdb65703bc047507478ceda302517`：独立桌面运行环境确认导入本仓库 `service.py`，参数输出 `300 30.0`：

```powershell
& 'C:/Users/81596/AppData/Local/QQSpaceInspector/runtime/Scripts/python.exe' -c 'from app.space_inspector import service; print(service.__file__); print(service.EXPERIMENT_BATCH_SIZE, service.EXPERIMENT_DELAY_SECONDS)'
```

旧 GUI 不热加载模块，需要先暂停、关闭后从桌面重开，再通过“载入已有任务”选择原任务继续。无需重建快照或重扫已完成成员。真实 300 人批次和新导出尚未验收。


冻结验证执行 SHA `2623031f7dabdb65703bc047507478ceda302517`，源码和测试在验证期间不变：

```powershell
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
uv run pytest --junitxml='C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260922-batch300/full.xml'
uv run python 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260922-batch300/summarize.py'
```

全量 2215 项：2198 passed、17 skipped、0 failed/error；主审 31 文件/262 项全通过；巡检内部回归 193 项中 192 passed、1 skipped（原有 Windows symlink 权限差异）。三门禁通过，格式 348 文件、类型 111 源文件。证据在上述 TEMP 目录 full.log/xml、summary.json、gates.json 和门禁输出。最终文档 SHA 推送后仍按同 SHA 查询 GitHub 三个 job，不将本机通过称为远程已通过；远程结果见最终交付回执链接。本批完成推送后释放仓库写入，不代推其他任务提交。


## 非好友访问维护页面

本轮接手基线 `9da4c1935fd8dce6b07b3219d93637558b338f0c`，拉取后工作区干净，另一仓库任务处于 idle；仅修改独立巡检，不重启生产服务，不动急停、群授权或动作开关。

负责人截图为登录后的非好友访问维护页，工具截图仍显示需要登录。只读任务核对表明是不同访问时刻：先记录 `unrecognized_page`，另一次记录 `login_required / login_redirect`，随后再次回到 `unrecognized_page`，不能把后一张已登录截图当作前一次没有跳转的证明。读取命令（执行 SHA 为接手基线，不证明旧 GUI 加载版本）：

```powershell
uv run python 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/inspect_saved_task.py' 'C:/Users/81596/AppData/Local/QQSpaceInspector/tasks/20260921T124031Z-621478da' --output 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/nonfriend-stop-read-20260922.json'
```

该截面总成员 993，已检查 538，明确限制 45、待确认 493、未完成 455（含受阻 1）。累计完成数不等于单批请求数，不能据此推断平台阈值。

验证浏览器第一次访问也跳转登录页；负责人正常登录后，通过 `nonfriendTab.getAXState()`、`goto('https://user.qzone.qq.com/<目标>')`、`playwright.evaluate(...)` 核对完整页面：`.page > .page_main > .error_content`、一个 `.report_img`，段落精确为“温馨提示:”“很抱歉,QQ空间相关功能升级维护,暂不支持非好友访问,敬请理解！”“返回我的空间”。目标地址、完整加载、查看账号均可核对。登录后跳转附带的查询参数未放宽，验证和巡检使用精确目标 URL；不复制登录凭据。

代码提交 `d734963662fd176a5009717fc9e7892e682ea3cd`：新增 `UNCONFIRMED / space_nonfriend_access_unavailable`、`qzone_nonfriend_page` 依据来源，存储保留完整提示原文。该页面不是违规提示也不证明账号正常；进入完整报告而不进入限制账号 CSV。目标、查看账号、完整加载和精确模板仍必需；未知维护/限频文字不能泛化为此页面，登录失效及 WAF 仍停止。300 人批次、30 秒间隔、已有记录和锁均不变，无数据迁移。

内部回归提交 `71b3adc0cd3f90ec288b90d2665ce5160a73da8c`。先将已核对原文的测试放在接手基线，源码恢复为干净基线后执行：

```powershell
uv run pytest tests/test_space_inspector_nonfriend.py -q -o addopts= --tb=short
```

复现 3 failed、17 passed，分别为页面未知、存储拒绝和续扫停顿；修复后同命令 20 passed。新增回归原样入库，包含身份/完整性、真实登录/WAF 优先、未知页面阻断、保存重开/导出、不得记为违规、旧暂停历史保留和检查下一个成员；主审探针未改。

真实 DOM 最小工具输出在本机忠实转录为 `QQSpaceInspector/evidence/nonfriend-template-dom-20260922.json`，不是原始 HTTP。冻结 SHA `71b3adc0cd3f90ec288b90d2665ce5160a73da8c` 运行生产分类器得到 `UNCONFIRMED / space_nonfriend_access_unavailable`：

```powershell
uv run python 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260922-nonfriend/verify_dom_transcription.py'
```

独立桌面环境已确认导入新契约。需关闭旧窗口、从桌面重新打开并载入原任务后继续；真实桌面越过本次暂停位置及新增导出仍待核对，不能以真实页面转录或合成测试替代。


### 本补丁实机续扫核对

负责人关闭旧窗口重开后反馈“已越过，继续检查了”。执行 SHA `71b3adc0cd3f90ec288b90d2665ce5160a73da8c` 只读核对：

```powershell
uv run python 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/inspect_saved_task.py' 'C:/Users/81596/AppData/Local/QQSpaceInspector/tasks/20260921T124031Z-621478da' --output 'C:/Users/81596/AppData/Local/QQSpaceInspector/evidence/nonfriend-resumed-read-20260922.json'
```

`2026-09-22T10:52:33Z` 截面：已检查 540/993，明确限制 45、待确认 495、未完成 453、当前受阻 0。原暂停成员新增 `UNCONFIRMED / space_nonfriend_access_unavailable`，保留完整维护提示原文；此前 3 条暂停历史仍在，并已检查其后成员。这证明本次桌面续扫通过该页面，不代表完成整群、无未来页面变化或新版 GUI 导出已验收。


冻结验证执行 SHA `71b3adc0cd3f90ec288b90d2665ce5160a73da8c`，期间仅更新文档，源码/测试不变：

```powershell
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
uv run pytest --junitxml='C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260922-nonfriend/full.xml'
uv run python 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260922-nonfriend/summarize.py'
```

全量 2449 项：2432 passed、17 skipped、0 failed/error；主审 31 文件/262 项全通过；巡检内部回归 213 项中 212 passed、1 skipped。三门禁通过：格式 368 文件、类型 115 源文件。跳过属于本次 Windows/uv 环境，不能直接与其他解释器或隔离 worktree 数字互换；本次 JUnit 及门禁原始输出保存在上述 TEMP 目录。新增内部回归已入库，旧主审探针未改。最终文档 HEAD 的远程 CI 另查同 SHA 的 run/job，不用旧版通过结果替代，交付回执附实际链接。


## 自动续批与性能优化（SPACE-OPTIMIZE-20260922）

接手基线 `b0b9c6c01d89b440fd7613350f5fd510ee565472`，已执行 `git pull --ff-only`、`git log -1`、`git status --short --branch` 对齐，确认原工作区干净。负责人批准现有巡检优化和直接接口验证，并明确暂停桌面巡检后再开展接口取证。辅助代理只读研究/审阅，root 单一写入；主审探针未改。

代码提交 `b6815b3cc814b0197d7e85113ee2258dd44fbd34`；内部回归提交 `df0a82b849b4a8ad598a2592f38ac99c1dc04c50`。改动仅为独立桌面巡检，不影响生产审核逻辑、群授权、动作开关、急停、数据库迁移或服务进程。

### 最终行为与使用

- 自动模式按已保存的固定待检队列串行运行，批间休息后继续。暂停、退出可中断间隔和休息，逐项保存；当前页面正在导航时须等有界导航结束。手动模式保留一批停止。
- 窗口默认 30 秒间隔、每批 300 人、批间 60 秒。可选间隔为 5/10/20/30/60 秒，批量为 10/50/100/300 人，休息为 60/120/300/600 秒。短间隔尚未获真实整群验证，不承诺吞吐倍率或免拦截，不采用并发、账号/IP 轮换、随机拟人或绕过验证。
- 轻量加载只过滤已观察到的静态资源命名空间中的图片/字体及指定头像路径；保留页面、样式、脚本及其他请求。采用 CDP 过滤而非 Playwright route，避免关闭 HTTP 缓存；浏览器不支持时退回普通加载。停止后复原；清理失败不会掩盖原登录/WAF 错误，保留可重试句柄。
- 不再因图片等资源超时直接隐藏已经加载的明确页面；同一页面有界等待完整状态，不自动反复请求。导航未提交时，旧的同 QQ 页面不能成为新观察。目标 URL、查看账号、精确系统模板和完整加载要求不变。
- 确认目标与查看账号、完整加载的未知页面仍先暂停。操作者可点“留待复查并继续”，本轮暂缓该成员并继续其余成员；该成员仍未完成，保留原记录。连续暂缓多个成员不会回到第一个形成循环；普通“继续检查”或重开任务会重新检查待复查成员。WAF、登录或身份异常不能这样跳过。
- 勾选“复用 24 小时内历史观察”时，同查看账号、同 QQ、同证据契约的近期完整观察可在其他任务中复用。保留原观察时间与来源任务/访问编号，不将复用时间当新观察时间，不延长有效期；新受阻记录成功写入索引后替换旧成功缓存；索引写入失败则本轮停用历史复用并提示。关闭复用即逐个实时访问；同一任务已有完成记录仍沿用原恢复规则。
- 本机 `observations.sqlite3` 只是可失效的索引，原任务是证据来源；索引损坏、文件类型异常或超限时提示并退回实时访问；过期或字段异常的单条记录按未命中处理并实时访问，不删除任务。首次运行会索引当前任务已有有效观察，不批量遍历所有历史任务。历史来源在界面和 CSV/JSON 中分别标识。
- 新版兼容旧任务和未封存快照的离线查看/导出；未封存任务仍不能扫描。数据表版本未变，依据中新增严格校验的 `reuse` 子对象。因此含复用依据的新任务不能交由旧程序读取，需继续使用新版，或在复用前保留任务副本供旧版使用；本轮未改写用户任务库。

安装入口与运行环境保持原样，本机独立环境已导入本仓库更新代码。旧窗口不会热加载；关闭后从桌面“QQ空间限制巡检”重开，载入原任务、确认登录，再继续。不需要重建任务或寻找隐藏的 AppData。

### 直接接口研究：证据与结论

研究执行基线为上述 `b0b9c6c`，浏览器取证使用 CUA 已登录标签页；没有复制 Cookie、登录令牌或原始 HTML 到仓库/日志。先使用 `optTab.goto(...)` / `reload()` 与页面快照核对已知目标，再通过 `optCDP.send('Page.getResourceTree')`、`optCDP.send('Page.getResourceContent', {frameId, url})` 读取当前主页响应。参数中的私人 QQ 号不在本记录公开；这不是后台新扫描引擎执行记录。

本轮观察的限制提示页、普通权限页、非好友维护页，各自在当前主文档 HTML 中含不同的明确提示，并能核对查看账号。这说明这些案例无需抓取相册、日志等内容即可读取提示；尚不足以证明登录失效、未知页面、正常主页等全部分支都可由独立 HTTP 解析可靠替代，因此本轮保留浏览器 DOM 判据，未发布纯 HTTP 批量引擎。

候选 CGI 来源为 [QZoneExport clients.ts](https://github.com/ShunCai/QZoneExport/blob/7e0218fab29316ca28b99a7c63deb0627002d271/core/qzone-api/clients.ts#L298-L305) 的 `cgi_userinfo_get_all`。公开项目用途为读取基本资料/访问权限，不能把权限失败或 `-4009` 直接等同违规；该项目的串行访问说明也不能证明接口免风控。

通过 `optCDP.send('Runtime.evaluate', ...)` 在浏览器内部进行一次同源候选请求：目标为已观察到限制提示的样本，带 `uin`、`vuin`、`fupdate` 和浏览器内部计算的 `g_tk`；页面未提供可用的 `window.g_qzonetoken`，该次未携带它。结果为 HTTP 404、非 JSON，未取得可用限制原因。此前语法失败和本地 token 前置检查没有发出请求；没有轮询/自动重试，也没有据此把所有 CGI 宣称不可用。参数完整的候选请求及阳性/权限/正常/受阻对照仍未完成，**直接 CGI 路线本轮未通过接入条件**。

资源过滤实现参考 [Playwright route 文档](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route) 的缓存说明与 [CDP Network.setBlockedURLs](https://chromedevtools.github.io/devtools-protocol/tot/Network/#method-setBlockedURLs)。过滤收窄至已观察的静态主机路径，避免将 API 查询中的图片后缀当资源地址。离线 Edge 测试证明缓存和 API 夹具请求保留，但不能替代真实所有页面覆盖。

### 冻结验证

执行 SHA `df0a82b849b4a8ad598a2592f38ac99c1dc04c50`，源码/测试冻结后运行：全量 2488 项，2470 passed、18 skipped、0 failed/error；原主审 31 文件/262 项全部通过；巡检 252 项中 250 passed、2 skipped。三门禁通过：格式 372 文件，类型 117 源文件。额外跳过包含主环境缺少可选 Playwright 的浏览器测试；同一冻结 SHA 已使用现有独立桌面运行环境执行该 Edge/localhost 测试，1 项通过。合成 Tk 控件操作与退出检查通过。原有其他 skipped 仍按该次环境/JUnit 记录，不当作通过或换用其他环境数字。

```powershell
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
uv run pytest --junitxml='C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260922-optimize/full.xml'
& 'C:/Users/81596/AppData/Local/QQSpaceInspector/runtime/Scripts/python.exe' -m unittest tests.test_space_inspector_lightweight -v
uv run python 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260922-optimize/verify_gui.py'
```

本机编排命令 `uv run python 'C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260922-optimize/verify_all.py'` 保存上述命令、SHA、退出码于 `checks.json`，JUnit 统计于 `summary.json`，各项原始日志同目录。浏览器回归已入库 `tests/test_space_inspector_lightweight.py`，只启动独立 headless Edge 访问本地合成 HTTP 服务；普通测试环境若无可选巡检依赖则明确 skipped，不能算作真实浏览器通过。GUI 私有验证仅用合成 worker，检查最小/默认窗口尺寸下控件边界、设置传递、忙碌禁用和退出，无真实 QQ 或任务读写。

最终文档 HEAD 的 CI 必须通过 `gh run list --branch windows-deploy-2026-09-10 --limit 5 --json databaseId,headSha,status,conclusion,url`、`gh run view <run-id> --json headSha,status,conclusion,jobs` 核对同 SHA，各 job 另列真实回执。不借用旧 CI 或未运行的测试结果。

实机边界：优化前的续扫反馈仍是历史证据；自动续批、短间隔吞吐、跨任务复用和整群持续完成尚需新版桌面验收。后续如被拦截，保留已完成结果并停止，不把暂停数量当腾讯固定阈值。
