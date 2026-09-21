# 电脑 QQ 空间限制巡检

任务：`SPACE-INSPECT-20260921`。负责人已接受首版只识别 QQ 空间明确限制提示，未命中保留待确认；正式使用不连接手机。此为明确追加的新功能，不是重开主审已关闭问题。

## 使用

桌面入口为 **QQ空间限制巡检**。代码在本仓库 `app/space_inspector/`，独立运行，不依赖 Codex，不需要启动另一个后台服务。

1. 打开工具，刷新群列表。群成员来源是项目已配置的机器人账号；QQ 空间观察账号由专用浏览器正常登录，两者分别显示。
2. 点击“打开空间登录”，在独立 Microsoft Edge 中登录 QQ 空间，然后点击“确认已登录”。首次登录和之后的会话过期需要本人处理；不复制现有浏览器或 QQ 的凭据。
3. 选择需要检查的群，点击“开始检查所选群”。每个任务最多选择 10 个群；同一账号跨群只访问一次，导出保留每个群的关联。
4. 可以暂停、关闭后载入任务继续。任务逐项保存；同一任务必须使用原成员来源账号和空间观察账号才能续扫。仅查看或导出已有任务不需要重新登录空间。
5. 点击“导出结果”，再点“打开导出目录”。`restricted.csv` 仅包含明确空间违规限制提示；`report.csv` 包含全部快照成员；`report.json` 保存观察依据、统计和群快照信息；`说明.txt` 解释范围与未完成数量。CSV 可用 Excel 打开。

不要在扫描期间手动切换专用浏览器的页面或账号。明确的“主人设置了权限”页面记为待确认并继续；遇到登录失效、访问失败或其他未知页面会保存进度并暂停，用户处理后才能继续。未完成任务也可导出，导出不会把未完成成员当作正常。

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

## 待验证

全群覆盖、不同权限页面、长期运行及 QQ 页面升级兼容性仍需实际使用验证。方案 A 主审裁定与 Windows 回滚维护窗口仍是独立待办，不因本功能完成而关闭。
