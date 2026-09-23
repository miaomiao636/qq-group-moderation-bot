# 项目交接文档 —— QQ 群多模态智能管理机器人（r132 评审轮）

**最新 CASE-BATCH-20260923：已推送并经负责人授权上线。** 部署提交 `22e60d1` 的应用源码与全量验证的 `d3e2d3c` 一致：案件摘要 CSV、冻结预览后批量 KEEP/CLOSED、撤回回执超时 UNKNOWN 均已加载。D 盘数据库备份校验、Web/Runtime 新进程、OneBot ready、自然消息恢复与配置不变已核验；急停 false、recall_only、群开关保持原样。未代用户结案、导出生产名单或重放历史动作。执行 SHA、命令、CI 与实机边界见 [本轮记录](2026-09-23-case-batch.md#授权部署2026-09-23)。N03 与整机故障恢复仍独立待办。

**最新 CARD-RECALL-20260923 已推送并上线**：源码 `23d0a1f` 补正真实群名片结构，追加微信小程序分享卡撤回，保留群主/管理员/成员白名单和校园墙二维码图片窗口政策。五个缺失群已启用；绵阳免费家教群 `628717263` 按负责人最新要求审核/动作双关闭。生产新进程、OneBot ready 与自然业务恢复已核验，急停仍 false、recall_only。执行 SHA、命令、数字、D 盘备份与实机边界见 [本轮记录](2026-09-23-card-recall.md)。本条与 §0 为最新状态，下文其他批次的“当前/未部署/急停 true”均为历史；最终文档 HEAD CI 按该记录及 §11 核验。

**N03-CAMPUS-20260923（只读补查）**：已生成 D 盘逐文件审核清单；当前媒体未发现完整到期可删项，未知源时间/无直接引用项仍保留，备份同字节覆盖不等于删除授权或 N03 闭环。校园墙图片后接文字、图片已取得真实窗口豁免记录，附文字发图作为来源仍待配对。执行 SHA `6ecb6d2`、命令、数量、审计修正和限制见 [N03 清单](2026-09-22-core-closeout.md#2026-09-23-处置清单补查只读未批准删除) 与 [线上窗口](2026-09-22-campus-source-scope.md#2026-09-23-自然窗口只读核验补充)。未清理原件、未改生产逻辑/开关、未重启服务。

**DAILY-BACKUP-20260922 清理补充**：负责人已授权清理三份 C 盘完整恢复副本，清单/收据另存、E 盘备份保留，后续完整恢复验收必须使用 D/E 盘。原文中的 C 盘恢复路径属于历史执行位置，内容已删除；执行 SHA、命令及空间变化见 [每日备份的清理记录](2026-09-22-daily-backup.md#c-盘恢复副本清理与后续位置2026-09-22)。不涉及 N03 或其他证据清理，主服务未重启。

**最新跟进 DAILY-BACKUP-20260922（已启用并完成首跑/恢复验收）**：不加密、排除服务密码和 Token；每日 04:30 的 QQBotDailyBackup 已注册，SYSTEM 首跑与完整隔离恢复成功。执行 SHA 4182767、全量/门禁/CI 和运行收据见 [每日备份](2026-09-22-daily-backup.md)。实时急停 false、主服务原 PID 与群授权/动作未变；下文 true 属历史快照。新备份不需要密钥，换机重填服务凭据；旧证据保留，整机演练待窗口，N03 未批准删除。


> **巡检最新优化（SPACE-OPTIMIZE-20260922）**：负责人授权自动续批、可调间隔、轻量加载、短期历史观察复用与未知页面留待复查；直接 CGI 尚未验证可用，主页 HTML 的已观察案例不能推为全群覆盖。默认 30 秒/人、300 人/批、批间 60 秒，可关闭自动续批。旧文“批末必须手动继续”仅属历史。执行 SHA、命令、兼容性及实机缺口见 [巡检记录](2026-09-21-space-inspector.md) 最新优化段。

**PROJECT-AUDIT-20260922：全面复查修复已推送并上线，急停保持开启**。冻结源码 `1c17ba09e5bef268a6162864830396e8d36c6107` 的全量、原主审、门禁与精确源码 CI 已验证；认证、群备注并发、连接资源、报告快照、备份边界与媒体依据修复及真实运维缺口见 [本轮审查](2026-09-22-project-audit.md)。生产已正常重启并加载 `1c17ba0` / `t204-v18`；备份、配置不变和自然消息核验见本轮审查的部署收据。急停 true；无生产清理、权限、计划任务或通知变更。以下旧批次保留历史，当时的“当前/未部署”不覆盖本条状态。

**此前 CAMPUS-TEMPLATE-20260922 上线记录（当前加载状态见上）**：当次加载 `06324fedab4baf8d0ee14e09991c5385627a01ed`，提示词 `t204-v18`。认可分享卡按图标、完整页脚和同卡布局联合识别；单图及普通文字配图均可建立同成员两分钟窗口。源码、真实原图复验、离线窗口回放、CI 与两次部署证据见 [本轮记录](2026-09-22-campus-source-scope.md)。未解除急停、未修改群授权或动作开关；新规则不重判历史消息。

**此前 CAMPUS-SCOPE-20260922 上线记录（当前加载状态见上）**：仅明确确认的万能校园墙来源保留豁免，通用小程序码恢复常规审核。加载 `8f4617edf6f73d8b98683994d575b022b9f2a0e4`、提示词 `t204-v17`；健康、新视觉记录、测试和 CI 的 SHA/命令见 [记录](2026-09-22-campus-source-scope.md)。

**A2-DEPLOY-20260922 已上线**：负责人明确授权“方案 A 上线”；真实备份副本验证后，已正常停服、迁移/回填、身份对账并恢复服务。当次 A2 加载源码 `53683d5998588f3b7490f4e73774df313b17b51d`，新 revision `e1c7d4b8a902`。命令、证据和未完成边界见 [上线记录](2026-09-22-authority-a2-deployment.md)。此前“生产未部署/待窗口”为历史状态；整机恢复与 N03 仍待办。

> **A2 独立候选历史（随后已上线）**：已裁定并实现同库决定和可重试导出。执行 SHA、命令、主审适配与隔离恢复见 [候选验收](2026-09-22-authority-a2-validation.md)。本分支有新 migration，生产工作分支继续旧 head；没有生产迁移、部署或开关变更。不要将本分支的代码存在等同于正式启用。

> **CORE-CLOSEOUT-20260922（主项目收尾）**：报告一致快照、UTC/区间与证据防覆盖修复已入库；已补固定窗口 shadow 报告、当前参数 B/C Mock 复测、合成库恢复和 N03 只读预检。具体 SHA、命令、数字与边界见 [收尾记录](2026-09-22-core-closeout.md)。主服务保持运行，通知按负责人最新决定暂不开启；N03 原件处置须先审核清单。负责人已授权本任务承担方案 A 审查和实施，不再等待外部裁定；方案 A 已在独立候选分支实现并通过本机验证，未授权生产迁移/整机停机；候选详情与发布边界见收尾记录。

> 面向接手的 AI/工程师。**读完这一份即可独立接手**：状态、资产、工作流、纪律、坑、待办、红线、协作约定。
> 生成日期：2026-09-21。所有数字均为**本机实核**（只读）或明确标注为"送审方声明"。

> **本轮新增授权（UI-PAGING-20260921）**：负责人要求群管理、成员白名单、报告待人工清单按页码管理，并显示群审核/动作配置数量。代码与验证进度见 [本轮记录](2026-09-21-admin-list-pagination.md)。账号巡检后续已收到样本，当前仅研究自动识别，见 §7.1。本轮是否已加载到生产以该记录为准，不能从代码提交推断。

> **后续追加授权（STABILITY-20260921）**：负责人要求查找并修复影响长期运行的问题。当前改动覆盖媒体/AI/心跳、缓存、人工结案事务和确认码、日期/候选分页、备份保底及数据增长边界，详见 [长期运行整改记录](2026-09-21-longterm-stability.md)。生产未重启；合并加载本批时需要 Web 与官方 Runtime，不能沿用分页单批的“仅 Web”方案。

> **后续制作（PHONE-INSPECT-20260921）**：负责人已授权制作独立手机辅助巡检工具，代码在 `app/phone_inspector/`，桌面入口已创建。功能、验证执行 SHA/命令、实机适配和限制见 [使用与验证记录](2026-09-21-phone-inspector.md)。后文早期“仅研究/未入库”是历史状态；是否完成有界实机及全群验收须分别看新记录，不能从已实现推断。该工具不改变生产机器人行为。

> **最新生产状态（MEDIA-QUOTA-20260921）**：负责人批准修复图片下载容量故障。已在线备份、将媒体配额设为 20 GiB，并正常重启 Web/Runtime；运行健康检查确认配额已加载、OneBot 重新就绪，后台分页也已生效。源码执行 SHA、全量与门禁、恢复验证及局限见 [媒体容量恢复记录](2026-09-21-media-quota-recovery.md)。此前“未重启/待加载授权”属于历史快照。手机巡检实机暂停，仍需继续验收。

> **恢复业务样本已取得**：自然收到的新图片已成功保存、视觉识别完成，对应撤回意图为 `SUCCEEDED`。执行 SHA、命令和记录关联见上述恢复记录；未自动补罚此前下载失败的图片，手机全群巡检和 Windows 回滚全链演练仍未验收。

> **SPACE-INSPECT-20260921 最新方向**：负责人已接受“空间限制巡检”首版，电脑选群、自动检查和导出，未命中待确认，正式使用不连接手机。独立桌面入口已制作；实现、真实页面观察、执行 SHA/命令、实机验收和 CI 各自状态见 [使用与验证记录](2026-09-21-space-inspector.md)。下文早期“等待自动识别读取方式/手机实演”保留为历史，不再作为当前方向。

> **最新巡检适配**：2026-09-22 新增非好友访问维护页适配：实页提示“QQ空间相关功能升级维护,暂不支持非好友访问”；此前被记为未知页面，同成员另一次访问曾实际跳转登录页。现按已核对的完整系统模板记待确认、保存原文并继续；不计违规、不推断账号正常，真实登录失效/WAF/未知页面仍暂停。300 人批次和 30 秒间隔不变。执行 SHA、命令及实机边界见 [巡检记录](2026-09-21-space-inspector.md) 的“非好友访问维护页面”段。

> **最新批次调整（2026-09-22）**：2026-09-22 负责人要求将每次开始/继续检查的上限由 10 人调为 300 人试用；保留批内间隔 30 秒、批末手动续批、手动暂停和遇到平台拦截/未知页面即停。此为试验批次上限，不是腾讯允许阈值，也不是加速请求。旧窗口需暂停并重开后载入原任务；300 人真实批次尚未验收。代码、执行 SHA/命令见 [巡检记录](2026-09-21-space-inspector.md) 的“每批上限调整为 300 人”段。

> **最新模板修复**：最新跟进：负责人续扫时遇到另一种官方限制提示模板（全角冒号、句号），旧识别器将其记作未知页面。已按实页核验补精确模板，原文保存/恢复/导出同步支持；不放宽账号、页面来源与加载校验。真实页面转录验证、只读任务进度及冻结版本测试见 [巡检记录](2026-09-21-space-inspector.md) 的“第二种限制提示模板”段。负责人重开后反馈已越过暂停位置，保存记录确认该成员追加明确限制观察且旧历史保留；新增导出待验证。整群可靠性和接口替代仍未验证。

> **当前限制**：整群连续扫描已遭腾讯 WAF 拦截，整群一次扫完验收未通过。已有进度和结果保留，程序明确停止而不把拦截算作成员异常；后续续扫已有新增记录，但平台稳定恢复及低频分批整体可行性尚未验证，不能交接成稳定全群批量工具。详见使用记录 WAF 段的执行 SHA、命令和实机证据。

> **最新实机跟进**：权限页修复后的实机续扫已保存更多进度，后续“对方未开通空间”模板也已适配；新增打开任务目录与具体暂停原因。各版本续扫/暂停/GUI 导出和整群验收分别留证，不能由自动化通过推断全群完成；执行 SHA、命令及范围见上述使用记录。

---

## 0. 一页速览

| 项 | 值 |
| --- | --- |
| 仓库 | `https://github.com/miaomiao636/qq-group-moderation-bot.git` |
| 分支 | `windows-deploy-2026-09-10`（长期工作分支，直接推送；**不要**推 main） |
| HEAD | 审核源码 `23d0a1f` 已推送并加载；最终文档 HEAD 以 git log -1 为准，运行字节核验见本轮记录 |
| CI | 源码 `23d0a1f` 的 run `35843307318` 最终三个 job 成功；合成 checkout/tree 与后续文档 HEAD 核验入口见本轮记录 |
| 测试基线 | `23d0a1f` 执行 `uv run --locked pytest --junitxml=D:/QQBotAudits/cards-groups-20260923/full-23d0a1f.xml`：2581 passed / 19 skipped / 0 failed/error，跳过原因见本轮记录 |
| 主审探针 | 同次全量 JUnit 子集：31 文件 / 262 passed，原探针未改动 |
| 静态门禁 | 同源码 `uv run --locked ruff check app tests alembic`、`uv run --locked ruff format --check app tests alembic`（357 文件）、`uv run --locked mypy app`（123 文件）通过 |
| 生产快照 | 审核源码 `23d0a1f` / `t204-v18`；急停 false、recall_only；>=200 的 72 群中 71 群开启审核/动作，绵阳为明确例外；执行 SHA/命令见本轮记录 |
| 当前评审协作 | 负责人已授权本任务兼任审查和实施；仍保留既有探针和证据循环，维护窗口与具体原件处置由负责人决定 |
| 当前卡点 | A2 已授权上线并恢复业务；N03 处置、整机故障恢复及缺失历史证据仍未验收 |
| 绝对禁止 | 启用 `enforce`、执行生产迁移/回滚、改判定逻辑与阈值、扩群或改动作开关（除负责人明确授权） |

---

## 1. 项目是什么

QQ 群多模态智能管理机器人：**双通道**（OneBot/NapCat + QQ 官方）群管理服务。

- **app/**（运行时代码）：`adapters`（通道接入）、`moderation`（规则+AI 判定、图片感知哈希）、
  `actions`（撤回/禁言等动作与编排）、`runtime`（管线、shadow 决策落库）、`cases`、`reports`、
  `notifications`、`web`（后台）、`core`（路由、配置、契约）。
- **入口**：`app/main.py`、`app/__main__.py`；服务日志在仓库根 `runner.out.log` / `runner.err.log`
  （日常操作见 `docs/windows-operations.md`、`docs/deploy-runbook-d037-d038.md`）。

**关键开关（接手时的文件配置快照；不证明当前进程加载值）**

| 变量 | 现值 | 语义 |
| --- | --- | --- |
| `IMAGE_HASH_MODE` | `shadow` | `off`（默认，零行为变化）/ `shadow`（只记录观察，**绝不改变判定**）/ `enforce`（命中即放行，**尚未实现、未授权**） |
| `ONEBOT_ACTION_STAGE` | `recall_only` | `recall_only`（只撤回）/ `full`（含禁言等，需授权） |
| `ONEBOT_SELF_ID` | `530297362` | 采集/执行绑定的账号（工具会校验，缺省即 fail-closed） |
| `DATABASE_URL` | 指向 `data/moderation.db` | SQLite |

> 权限/安全的硬约束：**色情/暴力与本地硬证据不被图片白名单豁免**；AI 判定阈值改动、`enforce`、
> 扩群、动作开关都属于**负责人**决策，AI 不得自行决定。

---

## 2. 状态快照（本轮与历史分开）

**CASE-BATCH-20260923**：部署提交 `22e60d1` 已加载，应用源码与全量验证的 `d3e2d3c` 相同。源码和部署前精确 HEAD CI 均成功；D 盘备份、新服务进程、自然消息及判定恢复、配置不变已实核。执行 SHA、命令、数量、原始失败整改和最新文档 HEAD CI 核验入口见 [案件批量与回执超时](2026-09-23-case-batch.md)。下方较早卡片版本是历史加载记录。

**CARD-RECALL-20260923**：源码验证、精确 CI、授权扩群/绵阳双关闭和生产重启均完成。详见 [源码、群授权与部署证据](2026-09-23-card-recall.md)。接口回执仅证明接口成功；新版群卡片自然实机样本仍独立验收，不从全量测试或普通消息恢复推断。

**SPACE-EXPORT-20260923**：独立巡检导出已与内部任务分离，新结果按群名/群号/时间命名，每群独立 CSV；旧任务 ID 和旧导出保留。代码 `8fceddedc95b1fea921a332211ab504294cb7490`、内部回归 `684b4eaf1c6d8ba889769da776c992f81fd62b6d`。本轮执行命令、结果与最终 CI 核验入口见 [导出记录](2026-09-21-space-inspector.md#导出目录与群结果辨识space-export-20260923)。不涉及生产服务加载或扫描逻辑。

**最新 DAILY-BACKUP-20260922**：源码 7b040b8 的全量、门禁与精确 CI、文档 HEAD 4182767 的 CI 已通过。执行 SHA 4182767 已注册每日 SYSTEM 任务，手工触发首跑和完整隔离恢复通过；完整 SHA、命令、收据和原进程/开关不变比较见 [每日备份](2026-09-22-daily-backup.md)。首次按时自动触发尚未到期，不称为已连续多日运行。

**本轮 SPACE-OPTIMIZE-20260922**：独立巡检优化的冻结源码/测试 SHA 为 `df0a82b849b4a8ad598a2592f38ac99c1dc04c50`；`uv run pytest --junitxml='C:/Users/81596/AppData/Local/Temp/qqbot-space-inspector-20260922-optimize/full.xml'` 得到 2470 passed、18 skipped、0 failed/error，原主审子集保持通过。完整命令、门禁、可选浏览器测试的单独执行与跳过说明见 [优化验证](2026-09-21-space-inspector.md#自动续批与性能优化space-optimize-20260922)。本机独立桌面环境可导入新版，真实自动续批与整群完成仍待验收；未因本轮改变生产加载版本。

**本轮 PROJECT-AUDIT**：源码 `1c17ba09e5bef268a6162864830396e8d36c6107` 已推送并授权部署。全量 `python -m pytest -o addopts= -q --tb=short` 为 2425 passed / 4 skipped；原主审 `python -m pytest tests -k r132_review -o addopts= -q --tb=short` 为 262 passed。新增回归和门禁/CI 逐项 SHA、命令见 [审查记录](2026-09-22-project-audit.md)。当前运行核验为 `1c17ba0` / `t204-v18` / 急停 true；具体命令与执行 SHA 见部署收据。

**2026-09-22 模板修正历史部署**：当次加载 `06324fedab4baf8d0ee14e09991c5385627a01ed`、`t204-v18`，急停 true。首次上线的自然消息发现普通文字配图被 `mixed` 类型过滤，已在本次代码补丁修复并再次部署。冻结源码的全量、原主审、门禁、离线真实响应回放、精确源 SHA 的 CI、配置不变和服务恢复证据见 [本轮记录](2026-09-22-campus-source-scope.md)。真实模型调用证据仍对应初始源码 `ff4d1a2`，本次只离线复用响应，没有追加远程调用；源码验证、进程加载、线上自然样本分别列证，不互相替代。

**CORE-CLOSEOUT 历史快照，最终源码 `59d15eceff32df54520d214b04bd0dc9647c7f63`**：全量/门禁/主审子集与 N03 新观察的命令及数字见 [收尾记录](2026-09-22-core-closeout.md)，下方较早数字为历史快照。当时 A2 尚为独立候选，随后已按负责人授权上线，见 [A2 上线记录](2026-09-22-authority-a2-deployment.md)。

**SPACE-INSPECT-20260921**：已新增独立电脑空间限制巡检，当前验证状态见 [本轮记录](2026-09-21-space-inspector.md)。生产主服务未因本功能变更；此前媒体恢复仍按其证据记录。

**最新 MEDIA-QUOTA-20260921**：执行 SHA `d7ceaf320a22599595ff0aca0200fb7b8f2afc77`，全量 1997 项（1981 passed、16 skipped、0 failed/error），主审 31 文件/262 项全通过；格式检查 329 文件、类型检查 101 文件。命令与证据见 [恢复记录](2026-09-21-media-quota-recovery.md)。负责人明确同意后已备份和正常重启 Web/Runtime，无数据库迁移或群开关变更。最终 CI 须按实际 HEAD 查询，不借用旧 run。

**后续 STABILITY-20260921**：代码提交 `4d79c3803ac0817b80bb8dae3277699845c4e9a3`、内部回归入库 `bfc027f2c203e75300966841a8a59299a1c73c10`，等价兼容补丁后的最终本机执行 SHA `6806210320d5c8c7810472255d0f665aa026f91b` 全量与门禁通过；CI 与生产生效边界见 [长期运行整改记录](2026-09-21-longterm-stability.md)。本批没有修改外部主审探针或历史证据，没有更改判定口径。

**本轮 UI-PAGING-20260921**：代码执行基线 `759dc5c827c2304720eb23ce05e6e704623c3e1a`，页码分页、群配置数量与相关回归已完成，本机全量和门禁通过，结果与命令见 [本轮验证](2026-09-21-admin-list-pagination.md)。推送后 CI 与生产加载分别记录；本轮没有重启生产、没有迁移或更改群开关。账号巡检的后续研究状态见 §7.1。

**接手核验补正**：执行 SHA `9b287271a3614da562c0f05d7407c2d28b98a552`，快照 UTC `2026-09-21T06:27:47.592471+00:00`。命令 `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-r132-intake-9b28727-90442e26/snapshot.py`：动作配置开启 67 群，均有匹配 owner/route；数据库 revision 与 `uv run alembic heads` 同为 `d4b7c1e9a502`；文件配置为 shadow/recall_only。全库当时留存观察 6688 条，其中 `matched=true` 17 条；同目录 `shadow-matches.py`（命令同上替换脚本名）复核这些命中均为 shadow。原始 SQL、JSON 和 `intake-review.md` 保存在同目录。这不是固定部署窗口统计，不据此推断实际动作效果；**“零命中，等待样本”已不成立**。

**以下为接手时保留的历史快照（原基线 `2e9e686`）**，不代表本次 UI 变更已加载，也不把历史生产数量当作实时值：

| 维度 | 事实 | 来源 |
| --- | --- | --- |
| 代码 | HEAD `2e9e686`，工作区干净，与远端同步 | 本机 `git` |
| CI | 上面三次 run 全绿（Ubuntu / Windows / clean runtime-deps 三 job） | `gh run list/view` |
| 测试 | 1895 用例全量通过；262 项主审探针通过 | 本机 pytest |
| 生产迁移 | `alembic_version = d4b7c1e9a502`（= 代码 head，已对齐） | 只读查生产库 |
| 生产群动作 | `provider_group_settings`：onebot **enabled=1 → 67 行**、enabled=0 → 3 行；无跨 provider 同号冲突 | 只读查生产库 |
| 生产可路由 | `group_action_owners` 68 行 / `group_provider_routes` 67 行 → **67 群全部可路由**（不可路由 0） | 只读查生产库 |
| 图片白名单 | `image_allowlist` **68 行，全部 enabled=1**；另有 **1 条"撤回"只存在于拒绝快照**（库中无行） | 只读查生产库 |
| 拒绝快照 | `data/moderation.db.rejections.json`：**1 条**，该条目**无 `state` 键**（历史格式 → 按兼容默认 `rejected` 处理） | 只读查文件 |
| 身份证据 | `docs/evidence/image-review/identity-*.json` 共 **5 份**；最新一份（修复后）为 `identity-20260920T093858Z.json`，`allowed=68 (mismatch 0) rejected=1 (mismatch 0)` | 本机 |
| 生产部署 | 服务已重启加载新代码；提示词 `v15 → v16` | **送审方声明**（未经主审现场重验） |
| 未证明项 | 见 §8 | —— |

**重要口径纪律**：`docs/` 里写"生产已部署/已迁移"属于**送审方声明**；主审会区分"声明"与"现场证据"。
不要把它当成已验证事实对外宣称，也不要反过来臆测生产没做。

---

## 3. 核心工作流：与主审的循环（最重要，先读这一节）

### 3.1 六步循环

```
① 主审交付探针包（用户放到桌面）
     C:\Users\81596\Desktop\qqbot-review-pr45-<sha>\<sha>\
       ├─ REVIEW.md            ← 主审结论（含编号项、残余、关闭标准）
       ├─ VALIDATION.md        ← 复跑命令与证据口径
       ├─ README.md
       ├─ probes/              ← 本轮新探针（可能是 .py）
       ├─ previous*/           ← 上一轮"不可变"原件（用于回归）
       ├─ audit/               ← 可选：对账工具 + JUnit
       └─ *.junit.xml
② 我方**独立复现**：把 probes 原样跑一遍，核对失败数是否与 REVIEW 一致
③ 探针**原样入库**：`tests/test_r132_review_*.py`（只加文件头；必要的平台/路径/调度适配**逐处登记**）
④ 整改代码 → 跑该文件组 → 跑全部 262 项 → 跑全量 → 跑门禁
⑤ 送审材料 + 回评：`docs/<date>-r132-roundNN-review-submission.md` + `...-reply.txt`（纯文本、可直接转发）
⑥ 分三次提交（代码 / 测试 / 文档）→ 推送 → 等 CI（Windows job 约 10-13 分钟）→ 把 CI 的 run/jobId 回填进送审材料
```

### 3.2 五条纪律（都是被主审抓到过、代价很高的）

1. **"本机跑过" ≠ "已入库"，更 ≠ "被 CI 覆盖"**。
   （第十二批我把外部目录跑出的 31 passed 写成了"已入库 + 同 SHA CI 覆盖"，被主审 T01 判为不实；
   正确写法：仓库 196 + **外部 31**，并说明 CI 不覆盖外部包。）
2. **口径必须能复算**：任何"X passed / N 文件"都要标明**执行 SHA + 命令**。
   （"24 passed" 只成立于 `228f276`，在最终 HEAD 上应为"23 业务项 + 1 项历史 AST 审计不适用"。）
3. **主审探针一律原样入库**；任何适配只允许改**输入 seam / 平台路径 / 线程调度**，
   **不得**改业务断言；每处适配要写在**文件头注释 + 送审材料**里。
   （已登记适配见 §9。）
4. **提交三分**：`fix(rX)`（只动 `app/`、`scripts/`）/ `test(rX)`（只动 `tests/`）/ `docs(rX)`；
   送审材料里给出三个 SHA + 各自范围。
5. **不重开已关闭项、不夹带新功能、不为凑测试数删旧测试**；用户明确要求"什么被问到就改什么"。

### 3.3 决策权与协作习惯

- **用户（负责人）是唯一决策者**：扩群、`enforce`、阈值、迁移、回滚、口径、是否重开某项。
- 用户通常以**单字母**回答（`a` / `b`）：给选项时必须写清**代价、风险、推荐项与理由**。
- 主审只出**审查报告与复现测试**，不出补丁；主审明确说过的边界要照抄进回评（例：
  "允许登记输入调度适配，但不能为跑探针放松锁"）。

---

## 4. 资产地图

### 4.1 `scripts/`（22 个工具）

| 工具 | 作用 | 状态 |
| --- | --- | --- |
| `apply_review_decisions.py` | 落库负责人对图片审核清单的结论（失败即停；写白名单 + 拒绝快照） | **可用**（本轮整改对象） |
| `image_allowlist_seed.py` | 白名单种子导入 + 拒绝快照读写 + `decision_lock`/`_snapshot_lock` | **可用**（权威读写入口） |
| `verify_approved_identity.py` | 生效名单/拒绝快照 ↔ 负责人所审文件的身份关联（只读证据） | **可用**（只读） |
| `image_review_export.py` / `image_review_sheet.py` | 导出审核清单 / 写审核结论 | 可用 |
| `image_allowlist_replay.py` | 未迁移表的候选回放 | 可用 |
| `enable_group_actions.py` | 批量开群动作（provider 限定、写事务内复核路由、审计） | 可用（**批量写入，需负责人授权**） |
| `authorize_group_routes.py` | 群→动作路由授权（写锁内复核） | 可用（同上） |
| `group_size_survey.py` | 群人数普查（绑定 `ONEBOT_SELF_ID`、写结构化快照） | 可用（只读） |
| `shadow_report.py` | 图片哈希 shadow 观察报告 | 可用（只读） |
| `window_stats.py` | 时间窗口统计 | 可用（只读） |
| `rollback_preflight.py` / `purge_drill.py` | 回滚预检 / 清理演练 | 可用（谨慎） |
| `reviewer_pack_verify_submitted_evidence_bytes.py` | 主审取证脚本的本地副本 | 参考 |
| 其余（`capacity_loadtest` / `sample_*` / `w2_*`） | 抽样、评测、报告类 | 按需 |

### 4.2 `tests/`

- 后续执行 `6806210`：**171 个测试文件、1955 项**；文件枚举和 pytest 命令见整改记录。16 skipped 实际为 13 项运行时锁、1 项缺真实样本、2 项符号链接能力限制。
- 其中 **31 个 `test_r132_review_*.py` = 262 项主审探针**，是"外部契约"的载体：
  **不要删、不要改业务断言**，只允许"登记式适配"。
- 命名约定：`test_r132_review_roundNN_*.py` 表示第 NN 轮主审包的入库版本。

### 4.3 `alembic/`

- 迁移链 head = **`d4b7c1e9a502`**（`add_image_allowlist`）。
- 启动门禁：`app.db.check_db_migrated()` 会比对"库 revision vs 代码 head"，**库落后即拒绝启动**
  （`tests/test_r132_review_unmigrated_startup_boundary.py` 已证明）→
  **任何新迁移都必须与部署同一窗口**。

### 4.4 `docs/`（评审轮文档序列，按时间读）

- 送审材料：`2026-09-18-r132-(remediation|round2..5)` → `2026-09-19-r132-round10-review-submission`
  → `2026-09-20-r132-round1[1-4]-review-submission`（**round14 是最近一批**）。
- 回评纯文本（可直接转发）：`2026-09-20-r132-round1[0-4]-review-reply.txt`。
- **方案 A 提案**：`2026-09-20-r132-image-decision-authority-proposal.md` + `...-ask.txt`（原提案历史；最新裁定与候选分支见主项目收尾记录）。
- 运行手册：`windows-operations.md`、`deploy-runbook-d037-d038.md`、`windows-delivery-checklist.md`、
  `deploy-config-reference.md`、`group-rules.md`。

### 4.5 `docs/evidence/`（证据资产，**不可删**）

- `image-review/`：**9 个批次目录**（`batch-*/`，含 `IMAGE_REVIEW.md`、`DECISIONS.json`、原图）、
  `exclude_hashes.txt`、**5 份身份报告** `identity-*.{md,json}`。
- `allowlist-samples/`（负责人确认放行的样本图）、`stats/`（群人数普查快照 `groups-*.json/.md`）。
- 其他：容量/评测/保留期历史证据目录。

### 4.6 `data/`（生产数据，git 忽略，**只读**）

- `moderation.db`（SQLite）、`moderation.db.rejections.json`（拒绝快照）。
- 只读访问姿势：`sqlite3.connect("file:data/moderation.db?mode=ro", uri=True)`。
- **绝不打印** `.env` 里的令牌/密钥；读取配置只取需要的键名。

---

## 5. 数据契约与坑

**本节双存储结构是 A2 上线前的历史说明**。当前决定/来源/操作版本/生效位由 SQLite 同库事务保存，JSON 仅为可重试导出；当前契约和迁移/恢复限制以 [A2 上线记录](2026-09-22-authority-a2-deployment.md) 为准。不得因下方历史“现状”描述切回旧写入工具。

### 5.1 表：`image_allowlist`

| 列 | 说明 |
| --- | --- |
| `id, phash(16), note(64), source(16), hit_count, enabled, created_at, created_by` | `phash` 唯一（64 位 dHash 十六进制） |

- **运行时只读 `enabled=1`**（`app/moderation/image_hash.py:167-169`）→ 加列/改工具不影响判定。
- 现状：68 行全 `enabled=1`；**被撤回的哈希在库里没有行**（只在拒绝快照里）——这是双存储问题的根源。

### 5.2 拒绝快照：`data/moderation.db.rejections.json`

```json
{ "<phash>": { "state": "rejected|approved", "source": "review:batch-...|exclude",
               "operator": "...", "at": "...", "first_rejected": "...",
               "history": [ {"state": "...", "at": "...", "source": "...", "operator": "..."} ] } }
```

**坑（必须记住）**：
- **历史条目可能没有 `state` 键** → 兼容默认 `rejected`（生产现存那 1 条就是这种；`verify_approved_identity._state_of` 已处理）。
- `state` 是**枚举**：`approved` / `rejected`；`null` / `true` / `7` / `""` 一律视为非法（fail-closed）。
- 文件**损坏 ≠ 缺失**：损坏必须显式报错，不能当空集、也不能覆盖原件。
- 写入用锁内读改写 + 原子替换（`_snapshot_lock`）。

### 5.3 批次目录：`docs/evidence/image-review/batch-*/`

`IMAGE_REVIEW.md`（表格：编号 | 状态 | 图片 | 来源 | 完整 SHA-256 | dHash | …）、
`DECISIONS.json`（`{"batch": <目录名>, "decisions": {"1": "放行|撤回"}}`）、原图文件。
**编号是正整数**（导出用 `:02d` 只是最小宽度，第 100 张是三位）。

### 5.4 双存储问题（C03 系列的根因，务必理解）

"决定状态"被拆在 `image_allowlist` 行 + 拒绝快照 JSON 两处，**没有共同提交边界** →
历史上出现"补偿覆盖后来的成功决定""最后一次读取到 commit 之间被穿过"等反例。
现状：用**跨进程 `decision_lock`** 覆盖"前态读取 → DB 变更 → 快照发布 → 补偿"全链（方案 B，已通过）。
**根治方案 = 方案 A**（见 §7 待办 1）：决定/来源/操作版本/生效位同库事务保存，JSON 仅作可重试导出。

---

## 6. 常用命令与 Windows 坑

```powershell
# 进入仓库
cd "d:\CodeBuddy工作空间\CB 项目\qq-group-moderation-bot"

# 全量测试（统计口径用 junit，避免被 -q 输出淹没）
uv run pytest -q --no-header --tb=no --junitxml=tmp_j.xml
uv run python -c "import xml.etree.ElementTree as ET; r=ET.parse('tmp_j.xml').getroot(); ts=list(r.iter('testcase')); print('TOTAL',len(ts),'FAIL',sum(1 for c in ts if c.find('failure') is not None),'ERR',sum(1 for c in ts if c.find('error') is not None),'SKIP',sum(1 for c in ts if c.find('skipped') is not None))"
Remove-Item tmp_j.xml          # 别把临时文件留在仓库

# 只跑主审探针（262 项）
uv run pytest tests -k "r132_review" -q --no-header -p no:randomly -p no:warnings

# 复跑主审给的外部探针包（注意：PYTHONPATH 与 -c pyproject.toml -o addopts=）
$env:PYTHONPATH=".;tests"
uv run python -m pytest -c pyproject.toml -p conftest "<包路径>\probes" -q -o addopts= --tb=no

# 门禁三条（与 CI 一致）
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app

# CI
gh run list --limit 3 --json headSha,status,conclusion,databaseId
gh run view <run-id> --json headSha,attempt,conclusion,jobs
Start-Sleep -Seconds 600        # Windows job 约 10-13 分钟；循环检查直到 completed
```

**Windows 环境的坑（都踩过）**

1. **pytest 通配符要用正斜杠**：`tests/test_r132_review_*.py` 有效，`tests\test_r132_review_*.py` 会报 "file or directory not found"。
2. **`Get-Content` 被安全策略拦**（读 `.env`、配置 JSON 时会报 SecurityException）→ 改用 Python 读，或只取所需键。
3. **PowerShell 引号地狱**：`python -c "…含 \" 的代码…"` 容易被吞引号 → 复杂片段用**单引号**包整段代码，或写**临时脚本**（用完 `Remove-Item` 删掉）。
4. pytest 清理临时目录时会打印 `safe-delete ... CONFIRM_REQUIRED` 噪声（不影响结果）。
5. 仓库里已有 `runner.out.log` / `runner.err.log`（服务日志），不要误提交改动。
6. 随机化插件：复跑主审包时用 `-p no:randomly` 固定顺序；官方全量用项目默认配置。

---

## 7. 未关闭项与待办（含前置条件）

**CASE-BATCH-20260923 已部署**：负责人选择现在上线后，已在 D 盘验证数据库备份并正常重启；原急停 false、recall_only 和群开关保持一致。未代用户批量结案或下载生产导出，首次真人使用结果仍需按实际操作核验。N03 来源期限权威索引、全副本继承、替代备份及具体处置清单批准未完成，不能因本轮上线而关闭。

**CARD-RECALL-20260923 更新**：新增卡片规则、缺失群授权和绵阳双关闭已完成；源码与生产恢复分别留证。新规则自然非保护卡片的实际撤回观察独立待验，具体边界见 [本轮记录](2026-09-23-card-recall.md)。整机演练、N03 和独立巡检待办不因本批关闭。

**巡检导出跟进**：负责人选择稍后实机验证新布局；重开窗口、载入原任务并导出即可，无需重新扫描。旧文件不搬迁/清理，真实整群持续可行性仍按巡检原有边界单独验收。

**DAILY-BACKUP 已收口**：QQBotDailyBackup 已启用，SYSTEM 首跑与完整隔离恢复已验收；新备份无需恢复密钥。日后核对 last_attempt/last_success 与磁盘空间，达到上限停止而不自动删除；QQ/邮件通知按负责人要求未配置。首次按时触发尚未到期，整机故障恢复与 N03 删除处置仍分别按下面前置条件处理。

**SPACE-OPTIMIZE-20260922 当前待办**：负责人重开独立桌面工具、载入原任务后核对新版设置、自动跨批和导出；短间隔吞吐及整群持续可行性另验。候选 CGI 本次请求未取得有效限制原因，参数完整请求及对照仍未验证，尚未接入；不能把权限失败当成员违规。已有自动续批、轻量加载和历史复用的实现/离线结果见巡检记录，不能将其当作上述实机验收。

- **PROJECT-AUDIT 已完成授权加载**：源码 `1c17ba0`，保留急停；部署收据见审查记录。日常备份/独立探针未发现匹配计划任务，历史 E 盘库仍为旧 schema；人工案件积压及选项见审查记录。整机演练、N03、通知的既有决定不变。

- **校园分享卡模板修正已加载，急停保留**：已包含于当前 `1c17ba0` / `t204-v18`，首次完整修正对应 `06324fe`。明确模板来源支持单图和普通文字配图，同成员后续普通文字、图片及文字配图沿用两分钟窗口；严重、刷屏、结构、不完整附件等保护保留。解除急停属于负责人操作，不由本轮测试自动触发；不重判或补罚历史消息。

**当前巡检方向**：负责人接受空间限制子集，正式不接手机；短量实机、全群与长期使用验收分别见 [本轮记录](2026-09-21-space-inspector.md)。不再以手机接入作为电脑版的前置条件。

| # | 事项 | 状态 / 前置 | Owner |
| --- | --- | --- | --- |
| 1 | **方案 A**（同库权威保存 + JSON 可重试导出） | 已裁定、实现并经授权完成生产迁移及加载，见 [上线记录](2026-09-22-authority-a2-deployment.md)；整机恢复演练单列下一项 | 已上线 |
| 2 | Windows **回滚全链实机演练** | 需负责人给**维护窗口** + 备份；**不在生产演练**（先合成库） | 用户排期 |
| 3 | "1 次未复现失败"钉死 | 需复现条件；至今未复现 | 我方 |
| 4 | shadow 观察**第二份报告** | 接手核验已有 `matched=true` 留存样本（§2）；已按固定 UTC 窗口整理，见 CORE-CLOSEOUT 记录；不能等同于真实动作证据 | 已完成报告 |
| 5 | 54→67 群补 owner/route | **已完成**（历史核验数字见 §2，不作为当前实时群数量） | 已关闭 |
| 6 | `enforce` 实现与阈值校准 | **未实现、未授权**；不得擅自开始 | 负责人 + 主审 |
| 7 | UI-PAGING-20260921：群/成员白名单/待人工清单分页与群状态数量 | 已随媒体容量恢复加载，运行证据见 [媒体恢复记录](2026-09-21-media-quota-recovery.md) | 已上线 |
| 8 | 异常账号巡检 | 已制作不连接手机的独立空间限制工具；全群连续扫描受 WAF 限制，长期批量可靠性未验收。具体状态见 [巡检记录](2026-09-21-space-inspector.md)，§7.1 保留早期手机研究证据 | 独立工具，非本轮主项目修改 |
| 9 | STABILITY-20260921：长期运行整改 | 代码与内部回归已入库，已随后续媒体容量恢复加载；本机/CI 见 [整改记录](2026-09-21-longterm-stability.md)，生产证据见媒体恢复记录。整机恢复演练仍单列待办 | 已加载，整机演练待排期 |

### 7.1 异常账号自动识别研究（2026-09-21，手机读取已实测，批量巡检未实现）

**负责人决定**：已提供桌面样本文件，明确选择“先不加名单功能，继续研究自动识别”；后续确认使用安卓 QQ、版本较新，全部样本当日查看仍弹出异常提示。初始版本未记录，后续已由设备读取，见下方实机记录。用户确认是人工标注，不等于本机取得了手机协议响应。不能把已知号码匹配包装为自动发现，也不能把“资料卡受限”直接解释为永久封禁、注销或违规事实。

**本机只读实验**（执行仓库 SHA 均为 `2758cdbf63143320602558582241649254abe332`；脚本仅在本机 TEMP，未入库、未纳入 CI）：

| 命令 | 实际结果与限制 |
| --- | --- |
| `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-account-sample-check.py` | UTC `2026-09-21T09:14:30.183504+00:00`；绑定配置中的机器人身份并核对登录账号。NapCat `4.18.19` 返回的成员列表包含全部 10 个样本，普通资料接口均返回 `ok` 和非空昵称；接口未透传原生 `result/errMsg`，不能推断原生响应无错。选取的 3 个比较成员未经人工确认正常。已观察字段未形成可靠区分，不能据此判定样本正常。 |
| `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-packet-readiness.py` | UTC `2026-09-21T09:25:46.065560+00:00`；`nc_get_packet_status` 返回 `status=ok, retcode=0`。只证明底层通道状态，不能证明特定资料查询受支持。 |
| `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-summarycard-probe.py` | 固定 Tars 编码向量、结构包装、响应解析及坏输入的离线检查通过；这是临时研究脚本自检，不是仓库新增测试或外部主审探针。 |
| `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-summarycard-probe.py --live-once` | UTC `2026-09-21T09:33:29.932983+00:00`；只向首个样本发出一次 `SummaryCard.ReqSummaryCard` 资料查询，无重试。请求外层版本为 3，HTTP/API 成功取得外层版本为 2 的响应（实验 JSON 误将响应外层版本字段命名为 `request_version`），`RespHead.iVersion=2`、`iResult=151`，脱敏提示为 `[oidb] error login sig,[url]`。这是该请求模板的登录签名校验失败，**不是目标账号异常或正常的结论**。 |

对应结果是同名 `.json` 文件，保留在本机 TEMP；样本号码、群资料、凭据与完整资料响应不入库。单次协议脚本 SHA256 为 `0c19604dbbfedba6981aec33f486acb7f891b9a974c3d4c92310ce7199bd46f8`，已有结果时拒绝重复执行或覆盖。未修改客户端、生产配置、群开关、案件状态或应用代码。

**已核对的源码证据与候选**：

- 运行版本对应 NapCat 固定源码 `af07479351c5b974e72ae1c7183f2272e79ffc1c`：成员转换把 [`unfriendly` 固定为 false](https://github.com/NapNeko/NapCatQQ/blob/af07479351c5b974e72ae1c7183f2272e79ffc1c/packages/napcat-onebot/helper/data.ts)，不能用它确认账号正常；[`get_stranger_info`](https://github.com/NapNeko/NapCatQQ/blob/af07479351c5b974e72ae1c7183f2272e79ffc1c/packages/napcat-onebot/action/go-cqhttp/GetStrangerInfo.ts) 未暴露手机 `RespHead`。本机成员缓存代码显示 `no_cache` 请求仍可能先返回已有缓存，因此本次“成员列表中存在”不等于服务器实时成员身份已获证明。
- [QAuxiliary 固定源码](https://github.com/cinit/QAuxiliary/blob/4b8fb59a4a2511c1f0e872aabcb61f257754d383/app/src/main/java/me/hd/hook/auxiliary/profile/RemoveGroupProfileDialog.kt) 将“群成员资料卡异常弹窗”与 `ProfileSecureProcessor.processProfileCard` 中 `RespHead.iResult=201/202` 直接关联；代码适配门槛为 Android QQ `8.9.88`，`9.0.0` 起调整方法参数。[TCQT 固定源码](https://github.com/callng/TCQT/blob/237601a89916e749ac399bba7935d5e2dc5c3e3b/app/src/main/java/com/owo233/tcqt/features/appearance/AllowViewingCard.kt) 还关联缓存 `Card.forbidCode/isForbidAccount`。这些是开源适配实现，不是腾讯官方码表或本样本的准确率验证；本轮只读源码，没有执行其中修改响应或解除限制的逻辑。
- 只读协议实验依据 [mirai 固定请求实现](https://github.com/mamoe/mirai/blob/283f8840d4682cc30fbdd87c66fe76f6a71ff8db/mirai-core/src/commonMain/kotlin/network/protocol/packet/summarycard/SummaryCard.kt) 的现有字段组合。使用当前 Windows 会话与历史通用入口 `eComeFrom=31`，没有模拟手机会话或猜测群入口参数。失败不能外推为其他合法查询方式都不可行。

**下一步与边界**：继续核对受支持的只读资料查询方式，以及读取安卓客户端实际可见提示的可行性；需要同一查看者、同一群入口、相近时间的异常与正常对照。没有稳定可验证的信号前，不实现自动账号判定，不按昵称、头像、等级、在线状态猜测，不将普通失败算作“失效”，不索取手机登录凭据、不绕过签名校验。查询成功但未命中候选码，也只能记录“未发现该信号”，不能承诺账号正常。未来若验证成立，才设计只读巡检与群名/群号/QQ 号导出。

**负责人已选手机辅助验证**：负责人接受连接安卓手机、保持 QQ 前台，先验证辅助巡检。优先评估独立的电脑＋手机工具；[Android UI Automator](https://developer.android.com/training/testing/other-components/ui-automator) 支持跨应用界面读取与操作。首轮只验证身份与提示对应；遇到锁屏、切换应用、身份不明或未知弹窗应停止。后续已取得少量实机页面，见下方；批量扫描、覆盖率与端到端导出准确性仍未验证。

电脑端已在本机 TEMP `qqbot-android-validation/platform-tools/` 准备 Google 官方 Platform-Tools；没有改系统 PATH 或安装手机组件。执行 SHA `2758cdbf63143320602558582241649254abe332`，命令 `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-android-validation/device-check.py`，UTC `2026-09-21T09:41:47.460717+00:00` 返回设备数量 **0**、没有读取手机界面；原始记录在该临时目录的 `device-check-20260921T094147Z.json`。这是连接前的历史快照；用户随后完成设备上的 USB 调试授权（[官方说明](https://developer.android.com/studio/run/device)），已继续实测。

**手机实机抽样（执行 SHA `96b8c450312b667dbb9a7ac1ef31a4bd2e706756`）**：原始证据仅保存在 `C:/Users/81596/AppData/Local/Temp/qqbot-android-validation/`，未入库、未纳入 CI。以下命令均在仓库目录执行，使用单个已授权 USB 设备；界面属于 QQ 应用分身 `user999`，不能假定与机器人的登录账号相同。

| 实际命令 | 观察与范围 |
| --- | --- |
| `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-android-validation/capture-device-metadata.py` | UTC `2026-09-21T10:09:30.846242+00:00`，设备已授权；Android `16` / API `36`，QQ `9.3.60` / versionCode `16070`。结果 `device-metadata-20260921T100930Z.json`；未保存设备序列号。 |
| `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-android-validation/read-qq-page.py` | 分次读取当前 QQ 界面。对提供样本中的 **2 个账号**观察到完整异常提示，并在正常点击“确认”后读到与目标一致的 QQ 标签；另 **1 个比较成员**的两次资料页采集均未出现该提示，只能记“本次未观察到”，不是正常账号证明。没有全群遍历，也没有以名单匹配产生判断。 |
| `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-android-validation/screenshot-qq-page.py` | 保存异常弹窗与底层 QQ 号码同屏截图，代理现场目视核对号码与后续 XML 标签一致；没有执行 OCR 或解除 QQ 资料限制。 |

可复核页面索引（每个 `page-<id>/page.xml` 均有同根目录的 `page-result-<id>.json`，记录时间、执行 SHA 和命令）：

- 群身份前后核对：`20260921T095742Z-943ac26e`、`20260921T100745Z-ef5eae50`，群名/群号一致。
- 首个样本：成员列表 `20260921T095826Z-94b29599` → 搜索页 `20260921T095900Z-2a5a9a55` → 结果页 `20260921T095935Z-58d63ee9` → 弹窗 `20260921T095949Z-1d235de0` → QQ 标签 `20260921T100021Z-f7075428`；同屏图 `screen-20260921T100004124007Z.png`。
- 第二个样本：结果页 `20260921T100556Z-de1f2085` → 弹窗 `20260921T100610Z-173cd8b9` → QQ 标签 `20260921T100625Z-62f0c80d`；同屏图 `screen-20260921T100614527197Z.png`。
- 比较成员：成员列表 `20260921T100638Z-3a21cc6f` → 资料页 `20260921T100651Z-c5d5004a`、`20260921T100728Z-03ae3986`，QQ 标签一致，没有异常提示。结束恢复群成员页 `20260921T100800Z-f388f31e`。

**证据边界**：弹窗 XML 本身不包含底层 QQ 号，搜索框也未暴露查询串；账号关联依靠同屏图及确认后 QQ 标签，群关联依据为代理现场观察的连续导航（`group_link_basis=agent_observed_navigation`），不是 XML 自动独立验证。采集脚本只读取当前页面；其报告 `automated_taps=0` 仅指脚本自身，整次验证确实由代理另行执行了搜索、打开资料、确认弹窗与返回操作。原始脚本和页面哈希可在本机封存时计算，但不能仅凭仓库 SHA 声称 TEMP 脚本版本已固定。

**当前结论与下一步**：已证实手机界面能提供可读取的异常提示，并可对应到具体 QQ。完整产品尚需验证群/成员身份链、重复昵称与列表复用、遍历结束条件、去重、暂停及中断恢复。输出应区分“观察到 QQ 资料卡账号异常提示”“本次未观察到该提示”“无法确认/未检查”；不输出永久封禁、注销或确认违规的推断，不联动处罚。手机已经恢复群成员列表，未安装手机应用、未操作生产配置、群开关或主项目判定逻辑。

研究文档提交 `96b8c450312b667dbb9a7ac1ef31a4bd2e706756` 的 [CI `35585015728`](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35585015728) 已成功：三个 job 实际 checkout `d93db36d2991e1083e1cc46b5620512a4ce14c98`，核验命令 `gh run view 35585015728 --json headSha,status,conclusion,attempt,event,jobs,url` 及逐 job 日志，记录在上述 TEMP 的 `ci-35585015728/verification.md`。这不覆盖 TEMP 手机研究脚本，也不证明生产加载；后续文档提交仍需核对自己的 CI。

---

导出多查询一致快照已由 CORE-CLOSEOUT 修复并新增并发回归；历史旧导出不被追认为同一快照。

## 8. 未证明项（`NOT_PROVEN`，**不能用本地测试顶替**）

A09 前 6 秒、历史授权快照、Windows 回滚全链实机演练、
一次未复现失败、`enforce` 最终批准与阈值校准、"B（补路由）"的实际动作效果。
审计里的 `stage_runtime_proven=false` 是**如实标注**，不构成"服务已加载 `recall_only`"的证据。

---

## 9. 已知"登记式适配"清单（改测试前先读）

**CARD-RECALL-20260923**：没有修改主审探针，新增本方脱敏合成结构与初始化事务回归；真实结构来源、未采用证据及测试命令见 [本轮记录](2026-09-23-card-recall.md)。

**SPACE-EXPORT-20260923**：新增内部回归 `tests/test_space_inspector_exports.py`；既有 `tests/test_space_inspector_optimization.py::test_partial_snapshot_can_be_viewed_and_exported_but_cannot_scan` 仅显式注入 TEMP 导出根目录，保留全部断言，避免新版 Service 将测试结果写到真实桌面。原外部主审探针未修改，旧 Store.export() 无参兼容契约仍被原测试覆盖。

**SPACE-OPTIMIZE-20260922**：本轮没有适配或改写外部主审探针。新增内部回归 `tests/test_space_inspector_optimization.py`、`tests/test_space_inspector_lightweight.py` 已随测试提交 `df0a82b849b4a8ad598a2592f38ac99c1dc04c50` 入库；Edge 夹具只访问本机合成页面。只读审阅发现的未封存快照载入、导航旧页面、连续暂缓和过滤清理问题已补回归，完整证据见巡检记录。

**PROJECT-AUDIT-20260922**：只新增内部回归，没有更改或适配现有主审探针。文件清单和执行证据见本轮审查记录。

**CAMPUS-TEMPLATE-20260922**：本轮新增 `tests/test_campus_share_template.py` 与 `tests/test_campus_mixed_window.py`，没有适配、删除或更改既有主审探针；真实原图与响应仅在私有目录留存，公开验证收据见本轮记录，不计作入库测试。下列为更早历史登记；上一轮 CAMPUS-SCOPE 的适配与封存原件见 [来源范围记录](2026-09-22-campus-source-scope.md#回归与原探针适配)，不是本轮新增适配。

| 文件 | 适配 | 原因 |
| --- | --- | --- |
| `tests/test_r132_review_group_enable_boundaries.py` | ①平台无关路径注入；②`setup` 改写入合法结构化快照；③`test_stale_survey_enabled_flag_...` 改走**真实 JSON seam** 并断言解析器读到 `True` | 生产移除 injected-loader 兼容分支；Windows 反斜杠；原用例未覆盖其命名前提 |
| `tests/test_r132_review_round11_c03_operation_identity.py` | 最后一个用例的**线程调度**改为"后一次审核被锁阻塞" | 方案 B 跨进程锁使原到达点不可达；**最终状态断言逐字保留** |
| `tests/test_r132_review_r9_write_residuals.py` | 并发用例调度同上（阻塞+顺序执行） | 同上 |
| `tests/test_r132_review_round12_c03_second_snapshot_commit.py` | 2 参数合并为 1 个"阻塞+顺序+最终一致"场景 | 同上 |
| `tests/test_r132_review_round12_all_candidate_evidence.py` | 无（verbatim） | —— |
| 入库时保留的其它 6 个文件 | 仅加文件头 + `ruff format` 风格（AST 逐节点一致） | 可复算：`ast.dump` 逐节点比对 |

登记纪律：**新适配必须同时写进文件头 + 当批送审材料**，并给出"AST 一致性/AST 差异函数"证据。

---

## 10. 红线（绝对不做）

1. 不启用 `enforce`、不改判定逻辑与阈值、不改安全例外口径（色情/暴力/硬证据）。
2. 不执行生产迁移、不回滚、不重启生产服务、不改动作开关或群授权（除负责人明确授权 + 有备份与回滚方案）。
3. 不删除/改写 `docs/evidence/` 下的证据、批次目录与主审探针；不为通过而放松断言或锁。
4. 不在文档里把"声明"写成"已验证"；不把"跑过"写成"入库"；不臆测生产状态。
5. 不打印/不提交任何令牌、密钥、`.env` 内容、生产库文件（`data/` 已被 gitignore）。
6. 不用 `git push --force`、不推 main、不跳过 hook；提交前必须 `git status` 干净。

---

## 11. 两个 AI 的协作约定（交接协议）

1. **单一写入者**：同一时刻**只有一个 AI** 修改仓库并推送。交接时用本文件 + `git log` 对齐，
   接手方先 `git pull` 并确认 HEAD。
2. **分工建议**（可按用户指派调整）：
   - A：**实现 + 回归**（`app/`、`scripts/`、`tests/`，跑全量/门禁/CI）；
   - B：**送审材料 + 回评 + 证据对账**（`docs/`，复跑主审包、登记适配、回填 CI jobId）。
   - 两人都要守 §3.2 的五条纪律；**改动前先读最新文件内容**（可能已被另一方改过）。
3. **交接检查单**（每次交接前逐项确认）：
   - [ ] `git status` 干净、HEAD 已推送、CI 最新 run 为 success
   - [ ] 最新源码 SHA 的全量无 failed/error；主审探针保持通过（当前执行 SHA、命令与数量见 §2 对应本轮记录，不沿用历史基线数字）
   - [ ] 门禁三条通过（ruff check / format --check / mypy）
   - [ ] 本文件 §2 的状态快照与 §7 的待办已更新（数字实核过）
   - [ ] 本轮新适配已登记进 §9 与送审材料
4. **冲突避免**：不要同时编辑同一文件；若必须，先各自 `git pull`，由后提交者合并并复跑门禁。
5. **交接点**：① 主审新探针包到达时；② 方案 A 裁定后；③ 迁移/回滚窗口前后。

---

## 12. 快速上手：三个最可能的入手任务

**任务 1（最常见）：主审又给了一个包**
```powershell
# 1) 读结论
Get-Content '<包>\REVIEW.md' | Select-Object -First 120     # 若被拦，用 python 读
# 2) 独立复现（核对失败数）
$env:PYTHONPATH=".;tests"; uv run python -m pytest -c pyproject.toml -p conftest "<包>\probes" -q -o addopts= --tb=no
# 3) 原样入库（只加文件头）到 tests/test_r132_review_*.py；复用 §9 的登记格式
# 4) 整改 → 跑该组 → 跑 tests -k r132_review → 跑全量 → 门禁 → 三分提交 → 推送 → 等 CI → 回填 jobId
```

**任务 2：A2 已上线后的维护**

A2 已完成授权部署，当前状态以本文件顶部和上线记录为准。旧审核写工具与 A2 不得混用；不要直接把新 revision 库搭配旧版本代码。后续迁移或恢复仍须明确维护范围并保护新增业务记录。

**任务 3：出 shadow 第二份观察报告**
接手核验已见 `matched=true` 的留存样本；先确定一致的 UTC 窗口再生成报告：
```powershell
uv run python scripts/shadow_report.py --db data/moderation.db --since "<UTC>" --until "<UTC>"
```
（注意：窗口支持小数秒；报告要区分"窗口内观察"与"全库累计"，不要把不同范围相除。）

---

## 13. 一句话总结

本轮分页及后续长期运行整改的代码、验证与生产加载分别以 [分页记录](2026-09-21-admin-list-pagination.md) 和 [整改记录](2026-09-21-longterm-stability.md) 为准；
**真正的工作面是"与主审的证据对话"**：复现 → 原样入库 → 登记式适配 → 小步整改 → 可复算的口径。
外部输入仍包括：**整机故障/回滚演练窗口**、**历史缺失证据与 N03 处置清单审核**；分页与稳定性修复已随 MEDIA-QUOTA 授权恢复加载，证据见恢复记录。当前新增的电脑空间限制巡检及其未验证边界见 [本轮记录](2026-09-21-space-inspector.md)，不以旧手机路线作为前提。
