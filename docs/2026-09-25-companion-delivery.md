# COMPANION-DELIVERY-20260925：公司配套交付候选

## COMPANION-READY-20260926

负责人要求继续补齐技术交付后，核对另一写入任务空闲、`git pull --ff-only` / `git log -1` / `git status --short --branch`，从干净基线 `0c82cd4a551f6129c779e971c4dd121b7697dccc` 接手。根代理唯一写入，辅助审查只读。以下是本轮状态，后文前轮的安装/备份/许可缺口为历史。

- 服务安装默认只读计划、默认 NapCat 仅 Web；显式 Apply 才配置，新安装要求 SHADOW/动作关闭。拒绝覆盖已有服务、检查外部命令退出码。NSSM 退出后由 Windows 服务管理器按 5 秒/15 秒两次重启、随后 NONE 处理，24 小时无失败重置；ctypes 配置并回读，不宣称真实服务故障演练已通过。
- 主库备份新增显式 bundle 来源，固定清单哈希、校验运行树和来源提交、确定性源码归档；恢复校验内层 ZIP 路径、集合、哈希、来源及数据库。原 git 配置默认值和历史加密/明文恢复保留，不从失败 Git 静默降级。
- 巡检独立离线备份显式数据根/导出根，锁住窗口与任务、包含所有任务、SQLite 已提交 WAL 快照、缓存原时间与账号关系、设置审计副本、当前/历史登记导出和旧任务内导出。拒绝重解析点、不完整/无法载入数据库、容量/时限超限、篡改清单或非全新恢复目标；不收集 browser、runtime、凭据，不自动覆盖 AppData。兼容旧版仅总表导出，未登记的历史导出位置不能保证覆盖。
- `pyproject.toml` 改为 MIT，依据既有 D-035，而非新选择；保留 LICENSE 版权文本。当前明确只给单公司 ZIP，不新建仓库或公开 Release。包收据新增清单哈希，手册相互引用并覆盖安装、账号关系、备份恢复和现场验收。

新增内部回归位于 `tests/test_bundle_backup_source.py`、`test_space_inspector_backup.py`、`test_service_installer.py`、`test_service_recovery.py`；打包测试追加清单哈希断言。开发时先复现缺失实现、损坏任务可被备份、数据库重复计容量、根目录额外模块、缓存口径不一致和收据缺字段，再修复。原主审探针未修改、删除或放松。PowerShell 安装测试使用假的服务查询与可执行程序；Windows API 测试为内存替身，不安装或停止真实服务。

本轮没有打开真实 QQ/NapCat、读取生产凭据或备份真实业务数据，没有生产服务、迁移、群开关、计划任务变更。公司账号登录、实际 Windows 故障恢复/备份调度、长时间巡检及人工暂停恢复仍需接收方现场验收。配置回读不能替代系统重启后的恢复实演。

### 本轮源码与本机验证

#### Windows CI 后续补正（以最终收据为准）

`8fc48d27ccfffa26bf004fcdbb3eef486f3f2b22` 的 [首次 CI](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/36218557352) 中，Ubuntu 与 clean runtime-deps 成功，Windows `uv run pytest` 失败：历史任务选择测试预期 50 条但返回 48 条。用 `gh run view 36218557352 --log-failed` 取证于 `ci-windows-failed.log`；不能把该候选包写成远端全绿。

检查发现历史读取把文件检查、连接准备耗时计入了 SQL 的 0.25 秒预算，会把有效任务当作无法读取而略过。新增固定时钟回归明确复现这种行为后，将预算起点移到连接准备完成、第一条 SQL 前；查询限额和 SQLite 中断门禁不变。原内部“最近 50 项”测试仅固定时钟以隔离磁盘/调度波动，实际数据库仍读取，50 项及首尾顺序断言全部保留；真实 SQLite 超时中断用例原样保留。这是内部测试环境隔离登记，不是外部主审探针适配；`tests/test_r132_review_*.py` 无修改。

本补正提交后，以干净提交执行 `uv run python -X utf8 D:/qqbot-delivery-check-20260926/recheck/run_checks.py`，其中全量命令为 `uv run pytest --junitxml=D:/qqbot-delivery-check-20260926/recheck/full.xml`，另执行下面同一组 ruff/mypy/差异门禁。实际执行 SHA、命令、结果以 `recheck/checks.json`、`recheck/summary.json` 为准；最终包及 `ci-final.json` 必须匹配补正后的最终 HEAD，不沿用失败的旧 CI。下面的 `9c618c6` 本机结果属于补正前已验证基线，保留历史，不冒充补正后的全量执行。

冻结源码/入库测试执行 SHA：`9c618c694ac8d68ec87ea5243bdb9cf7c2650894`。证据目录 `D:/qqbot-delivery-check-20260926/`；`checks.json` 记录命令与退出码，`summary.json` 从 JUnit 复算。全量启动时仓库干净；执行期间只补接收方手册和包描述文本，`app/`、`scripts/`、`tests/`、迁移及锁定依赖均未改变。

```powershell
uv run pytest --junitxml=D:/qqbot-delivery-check-20260926/full.xml
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
git diff --check
```

实际全量 **2788 passed、19 skipped、0 failed/error（2807 项）**；同次执行中的原主审子集 **31 文件/262 项全通过**，相关交付回归 **65 项全通过**。ruff check、format（402 文件）、mypy（130 源文件）与差异检查通过。跳过为生产服务持锁保护 13、私有样本缺失 1、链接权限 4、可选巡检运行环境缺失 1；未停生产以消除跳过。可选巡检依赖在下述隔离环境另外安装并验证导入，但没有把这次导入称为该浏览器测试已执行。

### 固定提交候选包的隔离验证

同一来源 SHA `9c618c694ac8d68ec87ea5243bdb9cf7c2650894`，执行：

```powershell
uv run python scripts/build_companion_bundle.py --ref 9c618c694ac8d68ec87ea5243bdb9cf7c2650894 --output D:/qqbot-delivery-check-20260926/candidate.zip
uv run python -X utf8 D:/qqbot-delivery-check-20260926/smoke_bundle.py
```

该中间候选包 SHA-256 为 `84ff545a0bdaeef592b5033f1923061b84cf651f0b5286cc29ca6ec84e2ad92a`，清单哈希 `8ebab988ce43974ea74fbd4689800a8e270373f98c1fb6c3dba4a6bce2b81844`。核对 **180 文件**与源码语法/哈希、**19 个包内文档链接**；D 盘无 Git 解压目录独立 `uv sync --locked --no-dev --extra inspection`，主应用、Tk、巡检及备份导入成功，反向确认 pytest 未安装。

在这个解压目录实跑安装只读计划、合成空库迁移/初始化、bundle 备份与完整隔离恢复、巡检合成任务/缓存/导出备份与隔离恢复；恢复后加载任务验证待检成员、账号绑定与原观察，确认没有浏览器会话或生效的旧导出设置。逐条子命令、退出码见 `smoke-commands.json`，结果见 `smoke-summary.json`。完整恢复副本均在 D 盘，不是生产数据或实际服务故障演练。

验证暴露并修正手册遗漏：空库完成 schema 迁移后，还需用现有 `backfill` 在预览无记录前提下初始化判定记录版本。缺失时备份原保护正确拒绝，未删门禁或手写标记。私有验证脚本初始误用 `APP_ENV=dev`，按实际枚举改为 `local`；初次 PowerShell 直接执行受本机脚本策略阻止，后续仅验证进程临时执行策略，不改变整机策略或服务；公司手册补 IT 执行策略说明。失败日志保留，未把失败命令计为通过。

### 最终包与远端核验方式

最终文档与元数据文字修订提交后，从最终 HEAD 再生成 `dist/qqbot-companion-<短SHA>-candidate.zip`，重新核对清单、链接及独立恢复流程；具体最终来源、ZIP 哈希、清单哈希与验包结果保存为 `final-bundle.json`、`final-smoke/smoke-summary.json`，不把上述中间包哈希冒充最终包。候选包和私有合成验证材料不提交 Git。

推送后使用 `gh run list --branch windows-deploy-2026-09-10 --commit <最终HEAD> --json databaseId,headSha,status,conclusion,url` 定位，再用 `gh run view <runId> --json headSha,status,conclusion,jobs,url` 核对 Ubuntu、Windows 和 clean runtime-deps，保存 `ci-final.json`。只有精确 HEAD 全部成功才报告远端通过；本记录不预写未来 CI 成功。公司实际新现场验收独立待办。

## 前轮交付记录（2026-09-25，历史）

负责人选择方案 A：群管理与空间巡检搭配交给同一家公司，并要求双方文件说明关系和配套方式。本轮范围为同仓库的接收方文档、固定提交候选打包及验证；没有新建仓库、发布 GitHub Release、改变仓库可见性、安装服务、访问 QQ 或修改生产数据。

## 产物与当前边界

接收方入口 `DELIVERY.md`，分别链接 `docs/delivery/group-management.md`、`space-inspector.md` 和 `acceptance-maintenance.md`。主 README 与巡检技术记录反向引用这些入口。接收方包不附带历史评审记录或六份工程上下文，避免把现场资料和历史通过声明当作新公司实测。

`scripts/build_companion_bundle.py` 从指定 Git 提交的允许清单读取原始 blob，生成带 `candidate_not_accepted` 状态的源码 ZIP。白名单包含应用、迁移、公开空值配置模板、规则资源、必要安装/维护脚本、接收方文档及现有 LICENSE；不读取工作区未提交文件或本机运行数据，不使用会受 export-subst/export-ignore 影响的归档过滤。非普通文件/链接、不明运行资源、缺必备文件、已有输出文件和非空模板凭据均阻止打包。每个包带来源 SHA、逐文件哈希/长度及发布阻塞清单；不宣称这是离线 EXE 安装包。

允许清单核对发现并显式包含 `alembic.ini`、`config/ai_prompt_rules.txt` 及备份动态导入的 `scripts/image_decision_authority.py`、`image_allowlist_seed.py`、`retention_audit.py`。`.env.example` 保留原始安全默认值，不把当前生产开关打进模板。

只读辅助审查指出旧 README 的 NapCat-only 双运行器描述有误，根代理核对 `app/main.py` 与 `app/runtime/runner.py` 后订正说明：主应用已承载 OneBot，`app.runtime` 需要官方机器人凭据。既有双服务脚本未修改，新手册明确不能作为 NapCat-only 一键安装入口。

正式交付仍需：服务安装模式适配、无 `.git` 源码包的可验证备份、巡检数据纳入备份并恢复、新机现场验收、统一许可标注以及确认发布范围。现有 `LICENSE` 为 MIT，`pyproject.toml` 为 Proprietary，未擅自替负责人选择。`gh repo view --json nameWithOwner,visibility,url` 实查现有仓库为 PUBLIC，当前不上传 Release 附件。

## 来源与验证

接手基线 `25daf5df1f9e96a9aca16a3981a5c3f210cb88e9`，经 `git pull --ff-only` / `git log -1` / `git status --short --branch` 核对干净且已对齐。根代理唯一写入，辅助审查只读。

- 新打包脚本首提交 `11cfaf1`，内部合成回归提交 `93840ed`；后续发布阻塞清单与 lint 调整后的冻结执行 SHA 为 `342b1346179c23f3fd48fe20ed6b0aa1035c731f`。
- 接收方文档提交 `6c191527efc023d785fbcdb37f20598dc5a70415`，不改变上述冻结脚本/测试或应用源码。
- 新内部回归为 `tests/test_companion_bundle.py`。未适配、删除或修改原主审探针。初次缺少打包入口的预期失败见 `red.log/xml`；模板非空凭据可进入包的预期失败见 `red-template.log`，随后加阻止逻辑并保留测试。合成源文件显式写 LF，避免不同系统文本写入换行导致夹具字节前提不一致；打包仍保留真实提交字节。

私有证据目录：`C:/Users/81596/AppData/Local/Temp/qqbot-companion-delivery-20260925/`。全量/门禁执行上述冻结 SHA，`checks.json` 保存完整命令与退出码，`summary.json` 从 JUnit 复算。实际命令：

```powershell
uv run pytest --junitxml=C:/Users/81596/AppData/Local/Temp/qqbot-companion-delivery-20260925/full.xml
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
```

该冻结 SHA 实跑全量为 **2721 passed、19 skipped、0 failed/error（2740 项）**；原主审子集 31 文件/262 项全通过；巡检子集 281 项中 279 passed、2 skipped。新增打包内部回归 7 项全部通过。跳过包括生产服务持锁保护 13 项、私有样本缺失 1 项、链接权限 4 项和可选 inspection 运行时缺失 1 项；未停生产去消除跳过。ruff check、format（395 文件）、mypy（127 源文件）均通过。没有把源码测试通过称为公司现场验收。

验包执行来源 SHA `6c191527efc023d785fbcdb37f20598dc5a70415`：

```powershell
uv run python scripts/build_companion_bundle.py --ref 6c191527efc023d785fbcdb37f20598dc5a70415 --output C:/Users/81596/AppData/Local/Temp/qqbot-companion-delivery-20260925/candidate.zip
uv run python -X utf8 C:/Users/81596/AppData/Local/Temp/qqbot-companion-delivery-20260925/verify_bundle.py C:/Users/81596/AppData/Local/Temp/qqbot-companion-delivery-20260925/candidate.zip
```

该候选包 SHA-256 为 `29df6b9dbe51311ce3b63510f304745cc50569b371284a0c60321803eca59eff`。实核 177 个文件，清单中的哈希/长度及归档成员完全一致；160 个 Python 文件语法检查、18 个文档相对链接、解压后主应用/巡检/备份支持模块导入均通过。校验在既有开发运行环境中执行，仅证明包完整性、文档链接与源码导入，**没有验证另一台干净电脑依赖安装、服务注册、QQ 登录或长期扫描**。初始私有路径检索曾把 uv.lock 哈希片段误当用户号，已按完整用户路径匹配修正，不改变包内容或主审测试。

最终供负责人核对的候选包从最后文档提交生成，放在 Git 忽略的 `dist/` 下，来源与包哈希以包内 `DELIVERY-MANIFEST.json` 和私有 `final-bundle.json` 为准；同样复验并保留 `bundle-check-<SHA>.json`，不将早期候选包哈希当作最终包哈希。

最终 HEAD 推送后，以 `gh run list --branch windows-deploy-2026-09-10 --commit <最终HEAD> --json databaseId,headSha,status,conclusion,url` 定位，再用 `gh run view <runId> --json headSha,status,conclusion,jobs,url` 核对精确提交与各 job。最终凭据保存为 `ci-final.json`；未成功前不能宣称 CI 通过。该 CI 与现场交付验收分别记录。
