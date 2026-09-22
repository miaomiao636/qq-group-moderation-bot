# CAMPUS-SCOPE-20260922：小程序码豁免限于明确校园墙来源

## CAMPUS-TEMPLATE-20260922：分享卡修正已提交、未生产加载

线上 `t204-v17` 必须读到品牌全文，未覆盖负责人认可的无品牌分享卡，因而图片及图后窗口均失去资格。数据库只读核查已确认实际误撤及急停后跳过动作，原始记录不修改。负责人随后提供原图并明确批准按「紫橙三点气泡图标＋完整两行页脚＋同卡左右布局」识别，不能仅看任意码、颜色或通用文案。

候选 `t204-v18`：新增严格结构 `campus_share_card`，区分 matched / uncertain / not_matched；逐项验证同一附件的图标、完整页脚文字、布局及页脚小程序码，保留旧品牌文字路径。正文普通方码与页脚小程序码可以共存；正文原始广告/诈骗类别保留，由既有本地政策判断是否豁免。未知模板或非法字段拒收；疑似模板转人工且不授予窗口。仅明确非校园的图片仍按内容正常审核。

图片与窗口共用同一本地豁免函数。新 `detail_json.campus_source_policy` 保存原本地证据、实际复核阈值和全部模型结果摘要；仅已完成图片来源豁免可生成，窗口重算完整门禁。不能由模型直接授予、不能跨附件拼证据、不能用窗口内普通图片续期；旧无上下文且原始类别非空的负例保持拒绝。无数据库迁移、新依赖、阈值/授权/动作/急停/通知/图片哈希模式变更。

复核同时发现并补测：模板豁免不得压过群卡/合并转发结构规则或藏在其他命中的刷屏/严重类别；缺少实际复核配置时不生成新窗口资格，不能中断主流程。既有主审探针和断言未改动。本轮只根代理写入，辅助代理只读复核。

负责人已单独批准最多 12 次现有模型识别复验，实际调用已完成且没有超额。原图及敏感响应只留本机私有验证目录，不提交真实联系方式或凭据；[脱敏验证证据](evidence/campus-template-20260922/validation.json) 保存执行 SHA、命令、输入/原始响应摘要与逐条结果。模板是认可外观，不能认证 AppID 或排除精心伪造。线上急停保持原样，解除急停需要负责人独立操作。

### 本轮验证与交付边界

以下均执行于冻结源码 **`ff4d1a2aa79f3ff2f6e88a1ef5acafe262fc731e`**；工作目录为隔离 worktree，Python 使用生产虚拟环境的解释器，环境为 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`。

| 检查 | 实际命令（省略解释器路径） | 结果 |
| --- | --- | --- |
| 全量 | `-m pytest -o addopts= -q --tb=short` | 2333 passed / 4 skipped |
| 主审探针 | `-m pytest tests -k r132_review -o addopts= -q --tb=short` | 262 passed / 2075 deselected |
| 新模板回归 | `-m pytest tests/test_campus_share_template.py -o addopts= -q` | 58 passed |
| Lint | `-m ruff check app tests alembic scripts` | 通过 |
| 格式 | `-m ruff format --check app tests alembic scripts` | 359 文件通过 |
| 类型 | `-m mypy app` | 113 文件通过 |

真实模型执行：`-B <私有目录>/remote_check.py p1 p2 p3 n1 n2`，随后 `-B <私有目录>/remote_check.py p4 p5 p1 p2 p3 n1 n2`。私有目录为 `%LOCALAPPDATA%/QQBotDeploy/campus-template-20260922-01`。五份校园分享卡原图累计八次识别均为 matched、图片 allow 且有窗口来源资格；两份反例累计四次均无校园资格：牛头码两次转人工，外卖免单图两次按既有政策判违规。不能写成所有反例均撤回，也不能从这批小样本推断总体准确率。正文原始 ad 类别保留，没有为通过而改写模型原始结论。

另以同一 SHA 执行 `-m pytest -c pyproject.toml -p tests.conftest <私有目录>/test_real_image_replay.py -o addopts= -q --tb=short`，12 passed：原图字节＋保存的真实响应＋实际本地图片引擎＋隔离数据库回放；正例后续 30 秒及 120 秒消息只记录、无动作建议，121 秒恢复常规审核；反例不授予窗口。回放禁止 socket 连接、固定 SHADOW，群/成员/后续文案是合成数据，图片哈希及 QR 名单为空。首次私有夹具把空列表误传给文字阈值参数导致失败，改正夹具构造后通过，失败日志保留；没有修改生产实现或放松断言。

**“跑过”与“入库”分开**：58 项确定性模板回归已作为新测试文件入库；真实原图、供应商原始响应及私有回放脚本未作为仓库测试提交，公开入库的是脱敏验证收据。既有主审探针在本轮没有修改。独立只读复核从保存的原始供应商响应重走解析、合并及来源判定，逐项一致，未再外呼。该核验命令为 `-B <私有目录>/independent-audit.py`。

源码已直接推送既有工作分支。[源码 CI 35702856001](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35702856001) 三个 job 全部 success；实际合并检出 SHA 为 `07eea15c61d69ca899b284831f2f1968e75094d6`，已核实包含源码 `ff4d1a2`。Ubuntu `106664719762`、Windows `106664719755` 的 `uv run pytest` 均为 2334 passed / 3 skipped；两平台 `uv run ruff check app tests alembic`、`uv run ruff format --check app tests alembic`（334 文件）、`uv run mypy app`（113 文件）均通过。干净依赖 job `106664719528` 通过仅运行时安装、应用导入和 pytest 不可导入的反向断言。核验命令 `gh run view 35702856001 --json headSha,status,conclusion,jobs,url,startedAt,updatedAt` 及 `gh run view 35702856001 --job <job_id> --log`；[CI 收据](evidence/campus-template-20260922/ci-source.json) 区分源 SHA 与实际执行 SHA。文档证据随后单独提交，不把该次 CI 冒记为后续文档提交的执行结果。

本轮没有生产部署、服务重启、配置修改或解除急停。只读生产核验 `-B <私有目录>/production-readonly.py`：生产 checkout 仍为 `b3ee1b0df4fdafa7a7561624ebc1a7748b26249d`，近期视觉记录仍为 `t204-v17`，数据库急停为 true，健康 HTTP 200、OneBot ready/connected；最近一次有部署收据的加载源码仍为 `8f4617e`。后续加载需更新代码与提示词标签、正常重启并核验新视觉记录；保持急停，不补罚历史消息。

## 上一轮 t204-v17 上线记录（历史，非本轮模板修正）

**上一轮已按负责人“现在部署上线”授权完成生产加载与验证。** 加载提交 `8f4617edf6f73d8b98683994d575b022b9f2a0e4`、提示词 `t204-v17`。首次提权取消属于历史尝试，证据保留；该次正常提权后部署成功，具体执行见下文。

冻结源码 `212488fb46a9b405bba4f3908b73d20a6223f138`：本机在 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8` 下运行 `<生产 .venv Python> -m pytest -o addopts= -q --tb=short`，2275 passed / 4 skipped；`-m pytest tests -k r132_review -o addopts= -q --tb=short`，262 passed；`-m pytest tests/test_campus_source_policy.py -o addopts= -q`，21 passed。Ruff check / format --check（357 文件）和 mypy app（112 文件）均通过，命令均为 `-m ruff check app tests alembic scripts`、`-m ruff format --check app tests alembic scripts`、`-m mypy app`。首次包装器只设 UTF-8 输出而子进程按 GBK 读取，导致 4 个迁移测试错误及解码警告；统一 Python UTF-8 模式后，未修改源码/断言的全量与主审重跑通过且无警告，失败日志保留。

源码 [CI 35683388560](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35683388560) 三个 job 全绿；实际合并检出 SHA `ef16100526e54d87d5ea48d36a2810391b1dd2a3`。Ubuntu `106604991700`、Windows `106604991861` 的 `uv run pytest` 均 2276 passed / 3 skipped，干净运行时依赖 `106604991838` success。核验命令：`gh run view 35683388560 --json headSha,status,conclusion,url,jobs` 和 `gh run view 35683388560 --log`。日志、校验收据和待执行部署脚本留本机 `AppData/Local/QQBotDeploy/campus-20260922-01`。

首次取消时生产工作区为 `6ef867a`、加载源码为 `53683d5`；现已由下文成功部署记录取代。没有向远程模型重发负责人截图，不能声称这些真实截图的模型准确率已验证；本次已验证确定性策略和合成回归。

负责人发现其他品牌的小程序广告也被 D-039 放行，明确选择 A：仅确认的校园墙来源保留豁免，其他小程序恢复正常审核，同时收窄图后窗口来源。本次替代 D-039 的“有码即通过”以及 D-036 联动中的通用小程序来源资格；不改变已确认校园墙的类别例外、两分钟窗口、阈值、群授权、成员白名单、动作开关、通知或图片哈希 shadow 模式。没有数据库迁移，不修改历史判定或重新处罚历史消息。

## 实现及可验证边界

- `campus_wall_source` 是可选枚举，只接受 `null` 或 `万能校园墙`；文字通道不能声明来源，非法值走现有模型降级路径。旧响应缺字段时无来源资格。
- 同条视觉结果须同时具有该来源值、严格 `校园墙白名单|文案:` 前缀和图内品牌文字。提示词要求明确品牌来源栏与完整一致的校园墙版式；码的形状/颜色、通用长按文案、正文提及或拼贴标识均不足以确认。该判断是视觉来源证据，**不是微信 AppID 认证，不能保证防止伪造或模型误认**。单有码且来源不清时仍按内容审核，不因来源未知判违规。
- 图片级豁免要求所有附件均符合来源契约，且原来的未决复核、色情/暴力、本地硬证据保护均通过。不能给其他广告附上一张校园墙图就整体免审。
- 图后窗口仅从新契约确认的来源取证；旧记录的 `小程序码通过` 及缺少新字段的旧校园墙标记均不具资格。同来源带码的既有诈骗窗口豁免保留，其他来源的广告/诈骗走现有审核门槛。
- 提示词、配置默认及模板同步升为 `t204-v17`；现有缓存键包含内置版本，新代码不会复用旧政策缓存。生产 `.env` 的版本标签在实际部署时单独核对，不能只凭默认值宣称已加载。

## 回归与原探针适配

隔离 worktree 基线 `6ef867aafea654e5e9f9ad667dc3ee17d664602f`。新测试先在未改实现上执行：`<生产 .venv Python> -m pytest tests/test_campus_source_policy.py -q --tb=short`，复现普通码覆盖广告/诈骗及旧记录授予后续豁免等预期失败；初版源码后已执行相关回归。正式冻结 SHA 和全量结果以随后追加的验证证据为准，当前段不宣称最终通过。

适配前文件逐字节封存在 [原件目录](evidence/campus-scope-20260922/original-tests/)，其基线 SHA、SHA256、变化函数与断言变化登记在 [适配清单](evidence/campus-scope-20260922/original-tests.json)。原先明确充当“合格来源”的合成夹具增加校园墙来源字段及品牌文案；来源文本转换集中在 `tests/campus_fixtures.py`，只用于这些合成正例。新通用码/旧记录反例直接构造原始输入，不走转换。

`test_r132_source_message_order.py::full_pair` 的来源行断言按新契约精确区分：全附件已确认时 `allow`，有附件缺校园墙身份时 `record_only`；原“不能跨结果拼前缀与布尔”的最终 `violation_high`/recall 断言不变。其反例通过显式移除带码结果的来源字段保留同条关联要求。其他适配不改变断言。未跳过或删除原探针，没有降低锁、阈值或未决复核要求。

## 发布与恢复计划

实现与测试先在隔离 checkout 完成，再直接推送既有 `windows-deploy-2026-09-10` 分支并核验 CI。正式加载需保存旧源码版本/配置副本和一致性数据库备份，等待空闲观察点，正常停止 Runtime/Web，同步经过验证的源码及提示词版本，启动后核验健康、连接、新消息和实际提示词标签。停止期间消息不保证补齐。回退仅恢复同 revision 的旧源码/提示词版本，不覆盖运行后新增的业务库；无授权不实际执行回退。**本段是计划，执行证据未追加前不代表已部署。**


## 生产部署已完成（2026-09-22）

负责人明确要求“现在部署上线”。部署前工作区干净；固定加载提交 **`8f4617edf6f73d8b98683994d575b022b9f2a0e4`**，与已冻结验证的实现提交 `212488f` 仅有文档差异。该加载版本 [CI 35684323434](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35684323434) 三项均 success；实际合并检出 SHA `f9149dc8d93b42f663a567237c0dd9a36b34f783`，两个平台执行 `uv run pytest` 均为 2276 passed / 3 skipped。核验命令为 `gh run view 35684323434 --json headSha,status,conclusion,url,jobs` 及 `gh run view 35684323434 --log`。

执行 `<生产 .venv Python> -B <私有部署目录>/deploy.py preflight`、提权 `pwsh -NoProfile -File <私有部署目录>/deploy-services.ps1`、`<生产 .venv Python> -B <私有部署目录>/deploy.py verify`。观察队列空闲后正常停止 Runtime/Web，生成一致性数据库备份并验证完整性，快进源码、仅将提示词标签由 `t204-v16` 改为 `t204-v17`，随后恢复服务。数据库 revision 保持 `e1c7d4b8a902`，无迁移、数据库覆盖或历史重放。

Web/Runtime 已运行，健康 HTTP 200、OneBot ready/connected；自然新消息已处理，新增视觉记录实际包含 `t204-v17` 和 `campus_wall_source` 字段。群授权、路由、白名单、阈值、动作与通知等配置摘要均与更新前一致。首次启动后连接尚未就绪、尚无新图片时验证按预期未通过；等待实际恢复与新视觉证据后才封存成功收据，没有放宽验证条件或发送测试消息。

脱敏 SHA、命令、备份哈希、服务时间线与自然流量观察见 [部署证据](evidence/campus-scope-20260922/validation-deployment.json)。私有配置与数据库备份位于本机 `AppData/Local/QQBotDeploy/campus-20260922-02`，此前取消与本机测试日志留在 `campus-20260922-01`；不提交原件、凭据或真实消息内容。提交此验收记录仅改变文档，运行服务仍加载上列发布提交。

本次未把负责人截图重新提交远程模型，不宣称真实截图准确率已验收。新规则适用于后续消息，历史判定保留；视觉来源不等于微信 AppID 身份认证。整机恢复演练、N03 原件处置及通知目标仍保持此前待办边界。
