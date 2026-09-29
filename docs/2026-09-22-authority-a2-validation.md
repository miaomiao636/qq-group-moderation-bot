# A2 候选审查与验证

> **后续状态：已生产上线。** 负责人另行明确授权“方案 A 上线”，本次维护及恢复证据见 [上线记录](2026-09-22-authority-a2-deployment.md)。下文“候选/未部署/待窗口”描述此前状态；新的生产事实以上线记录为准。

任务 CORE-CLOSEOUT-20260922。负责人授权我方同时主审和实施方案 A，要求沿用项目、不大改；Windows 整机演练继续待排期。本记录属于独立候选 `codex/r132-authority-20260922`，**不是生产迁移、部署或启用证明**。

## 结果与范围

图片审核决定改为同库权威：状态、来源、操作者、版本、生效位与历史在一个事务提交。JSON 是可重试派生导出；导出失败明确返回 pending/非零，不撤销已提交决定。普通 seed 不能覆盖人工拒绝；显式重批才可改变决定。回填默认 dry-run，矛盾或非法历史整批拒绝，未知审核时间保持 NULL。完整裁定及切换纪律见 [方案](2026-09-20-r132-image-decision-authority-proposal.md)。

沿用 SQLite/SQLAlchemy 和已有命令入口。线上判定路径、阈值、启动 revision 门禁、enforce、群授权和动作开关未调整。模型增加字段与新迁移意味着上线必须同窗口协调；不能把候选先合入正在运行的 checkout 后再等数据库迁移。

核心源码冻结 `90c1c9e5d7ccc35c434b032ebd14aae1a79ea45b`。后续 `ab7d9c7` 仅 N03 预检修复；`7456d40b61f969c41662665a971dee8c71bdfb40` 修复身份报告换行并增加布局回归。候选再合入工作分支形成 `5435e799e6400d40d70fa0c64a7fe7adf4effec8`，相对 `7456d40` 的源码变化仅为已授权独立巡检任务的批次参数与边界测试；不计入主项目交付功能。最终组合仍须单独验证，不能借用前轮结果。

## 原探针和内部回归

旧基线 `adef6ee558be3ba9d53de7111c6eecfd21a74ef9` 的外部探针原字节封存在 `docs/evidence/authority-a2-20260922/legacy-probes/`。本候选采用已裁定的新事务语义，变更逐项见 [适配登记](2026-09-22-authority-a2-adaptations.md)、完整 diff、AST 表和 Git blob 哈希补充。没有删除外部原件、skip/xfail 或放松并发保护；不能将适配版称作原样探针。

内部 A2 回归覆盖 CAS 旧版本冲突、初始化标记、拒绝记录、旧历史与备注保留、损坏导出封存、完整快照校验、线程/进程协调及导出重试。数据库中的非法状态/来源/历史直接注入后 fail-closed，避免只因 JSON stale 而产生假绿。报告换行缺陷用失败回归复现后修复。核心与报告修复均已入库，不以 TEMP 运行代替入库。

## 本机全量验证

执行源码 `7456d40b61f969c41662665a971dee8c71bdfb40`，工作目录为候选 worktree；使用生产 checkout 的 Python 可执行文件但 `PYTHONPATH` 指向候选，没有复制 `.env` 或生产库、没有同步共享环境。令 `<P>` 为 `D:/CodeBuddy工作空间/CB 项目/qq-group-moderation-bot/.venv/Scripts/python.exe`，`<T>` 为本机 TEMP 下 `qqbot-a2-final-7456d40-1a57538c`：

```powershell
& <P> -B -m pytest -p no:randomly -p no:cacheprovider --junitxml=<T>/full.xml
& <P> -m ruff check app tests alembic scripts
& <P> -m ruff format --check app tests alembic scripts
& <P> -m mypy app
```

全量 **2257 项：2253 passed、4 skipped、0 failed/error**。适配主审子集 262 项全部通过，内部 A2 与布局回归 37 项全部通过。格式检查 354 文件、类型检查 111 文件。执行前后源码哈希无变化、源码 diff 为空。四项 skip 分别为真实图片样本未挂载和三个宿主符号链接条件不满足；没有运行中生产锁跳过，因为本轮使用隔离 checkout。精确命令、节点、原始输出哈希见 [验证记录](evidence/authority-a2-20260922/verification-7456d40/run-metadata.json)。

这份记录对应上述执行 SHA；合入工作分支后的最终组合验收与 CI 应另列，不把旧数量直接改贴到新提交。

## 独立合成迁移与恢复

实际执行源码 **`90c1c9e5d7ccc35c434b032ebd14aae1a79ea45b`**，命令：

```powershell
& <P> -B <TEMP>/qqbot-a2-frozen-acceptance-7ae7d444ffc7/a2_acceptance.py --sha 90c1c9e5d7ccc35c434b032ebd14aae1a79ea45b
```

包装器、分阶段命令/返回值、摘要、来源 manifest 与补充读回均入库于 [合成验收目录](evidence/authority-a2-20260922/synthetic-acceptance-90c1c9e/summary.json)。完整数据只由包装器生成，未读取生产库、`.env`、媒体或真实凭据，未启动生产服务、未发起外部网络访问。

- 实际 Alembic 旧 head 建库与新 head 升级，旧字段/其他业务表保持；启动门禁对错配版本拒绝。
- 持有 WAL 写入后验证 online backup 保留已提交数据；只复制主 DB 的对照会遗漏该行。
- 未回填时决定写入拒绝；非法时间/历史、空决定、状态矛盾、未知标记全部拒绝；合法回填不改变原 enabled 集合，重复执行保持幂等。
- 旧快照未知字段、原行字段和历史保留；未知 decided_at 为 NULL；新增拒绝行 created_at 位于实际运行窗口，并经 ORM 读取。
- 新库通过新启动门禁；原旧代码面对新 revision 拒绝启动。TEMP 兼容桥接由冻结旧运行时加新模型/迁移/决定工具组合，来源逐项固定，通过同 revision 启动门禁和实际 enabled 查询。
- R2 只在额外合成副本执行降级并核对旧字段、其他表与旧启动门禁；完整新决定历史保留在 A2 库和备份，**不是无损降级**。
- 预迁移一致备份恢复到新目录后，以冻结旧代码核对逻辑内容及启动门禁，integrity/FK 检查通过。

封存 manifest 中 `No ... migration ...` 的范围措辞应读作“未执行生产迁移”：本轮确实执行了 TEMP 合成库迁移。原件保持不改，此处补正。后续提交只改变 N03 预检和报告布局等外围文件，迁移/模型/权威核心/启动门禁/备份源码与本次冻结一致；因此核心证据可复用，但不能把实际执行 SHA 改成后续提交。

桥接目前只是 TEMP 组合包，未制成正式回滚发布包；上述 startup gate 也不等于完整 Web/Runtime 或 Windows 整机启动验收。真实备份、凭据/媒体、断网断电、实际维护窗口与生产迁移耗时仍未验证。

## 发布条件

候选上传 GitHub 并通过 CI 后仍保留 draft，不自动合并部署。生产工作分支继续旧 migration head。正式切换前需负责人确认维护窗口，并完成预迁移一致备份、停止旧审核写入者、真实库只读预检/回填 dry-run、同窗口迁移回填与全量写入口切换、导出和身份对账、Web/Runtime 健康与数据对账。旧 O_EXCL 写锁和 A2 OS 锁不能混用。

通知渠道保持关闭，N03 原件不删除，enforce/判定阈值/群授权/动作开关不改变。未获维护窗口前，本候选的完成范围是实现、审查、隔离验证与可追溯送审材料。

## 公开证据口径

上述新增合成验收和验证记录为公开副本：仅把 Windows 用户目录名替换为 `<local-user>` 并统一 UTF-8/LF。原件留在本机 TEMP，未覆盖；原件与公开副本的哈希逐文件见 [副本映射](evidence/authority-a2-20260922/public-copy-manifest.json)。记录内原日志哈希仍指向本机原件，不将脱敏副本伪称为逐字节原件。外部主审原探针不做这类脱敏或换行变换，仍按原件 manifest 单独核验。

## 最终组合冻结验证

执行 SHA **`5435e799e6400d40d70fa0c64a7fe7adf4effec8`**，候选 worktree 中命令与上轮一致，JUnit 输出目录改为 `<TEMP>/qqbot-a2-final-5435e79-3c851600/full.xml`：`<P> -B -m pytest -p no:randomly -p no:cacheprovider --junitxml=<T>/full.xml`；扩展 `ruff check app tests alembic scripts`、`ruff format --check app tests alembic scripts`、`mypy app` 均通过。

最终组合 **2258 项：2254 passed、4 skipped、0 failed/error**；适配主审 262 项、A2 与布局内部回归 37 项全部通过。格式 354 文件、类型 111 文件。跳过原因与前轮一致，执行前后源码哈希相同、源码 diff 为空。新增加的独立巡检边界用例解释了总数变化，未混作 A2 回归。原始证据哈希与公开副本口径见 [最终验证](evidence/authority-a2-20260922/verification-5435e79/run-metadata.json) 及同目录副本映射。此后提交只整理记录，不改执行源码。

GitHub 最终 HEAD 的 CI 应以本候选 draft PR 最新 Checks 实核：Ubuntu、Windows 与 clean runtime-deps 必须分别成功；本机通过不代替 CI 或生产验收。
