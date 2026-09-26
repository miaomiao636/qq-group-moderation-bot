# 群名片补正、微信小程序分享卡撤回与含 200 人群授权

负责人 2026-09-23 明确要求「微信小程序分享卡以后也撤回，包含 200 人的也开」。本次只修卡片结构识别并扩充明确核对的缺失群；成员白名单、群主/管理员豁免、校园墙二维码**图片**政策和两分钟图片窗口不变。微信小程序**分享卡**无来源豁免，仍经过现有未知内容/缺失媒体等安全门禁。动作阶段保持 `recall_only`，不重放历史消息。

## 事实与证据边界

执行基线 `2391bcaec843a39ef34446db51aae87665723c82`，命令 `.venv/Scripts/python.exe -X utf8 -B D:/QQBotAudits/cards-groups-20260923/audit.py`，只读收据 `audit.json`，观察时间 `2026-09-23T09:00:05Z`：

- 141 个目录群中，人数 >=200 的 72 个，完整授权 67 个；缺失下表五群的 settings/owner/route。
- 当日已处理的合并转发 18 条：17 条撤回接口返回成功；1 条因大学生创业交流群动作未启用而跳过。接口成功不等于独立手机端观察。
- 用户截图群的群名片、微信小程序卡均被旧代码识别为普通 share_card，只记录；不是所有合并转发都失效。截图内小程序卡也不是合并转发。
- 该观察时急停为 false，动作阶段声明 recall_only；生产原进程加载历史源码 `1c17ba0`，不能把当前工作树 HEAD 当作已上线。

| 群号 | 群名 | 当时人数 |
| --- | --- | --- |
| 822284640 | 大学生创业交流群 | 210 |
| 784172346 | 徐州免费家教群 | 200 |
| 594188592 | 无锡免费家教群 | 200 |
| 609631435 | 合肥免费家教群 | 200 |
| 245106525 | 济南免费家教群 | 200 |

同一基线加工作树补正，以下只读命令均使用上述 Python 前缀：

- `D:/QQBotAudits/cards-groups-20260923/read_ark_schema.py`：本机 QQ 只生成 Ark，不发送群消息；生成的群名片为 `com.tencent.contact.lua / contact / meta.contact.jumpUrl=mqqapi://card/show_pslcard?...&card_type=group`。这是真实 QQ 生成格式，不冒称取回了截图历史原始载荷。
- `D:/QQBotAudits/cards-groups-20260923/read_recent_schema.py`：身份校验前后包围的保留消息查询，取得 `com.tencent.miniapp.lua / miniapp / meta.miniapp`，`tag=微信小程序` 且 `tagIcon=https://miniapp.gtimg.cn/public/miniwx.png`。三条消息为同一模板，不当作三个格式变体。仅保留结构摘要，不发布用户卡片载荷或签名参数。
- 旧截图消息查询返回空 message，无法据此断言旧原始结构；自然 inbox 观察也未捕获 JSON。`schema-live/local-napcat-source.json` 摘录与其说明不符，**未采纳**，原件保留；采用根目录的实际 API 收据，不引用错误行号。

## 实现与验证

`tests/test_wechat_card_recall.py` 保存合成脱敏结构及行为回归，不改动主审探针。覆盖普通新闻/音乐提及小程序、QQ 小程序、个人卡、重复参数、多卡顺序、保护身份、窗口隔离、管线去重、SHADOW 零动作及 recall_only 单次撤回。`R_WECHAT_MINIPROGRAM_CARD` 使用独立规则号，复核门、AI、配对窗口、图片白名单 shadow 与离线工具共享结构硬证据集合。

`scripts/initialize_group_actions.py` 默认只读。必须逐个指定群且执行时显式填写负责人授权；实时核对账号与含 200 人人数，D/E 盘备份、写事务内复核所有前态，再同时新增 settings/owner/route。任何通道已有行、账号/阶段漂移、审计写失败均阻止整批。准备审计不等于提交证据；实际提交以数据库审计和独立提交收据为准。不是以后自动按人数扩群的定时规则。

## 冻结源码验证与 CI

以下命令均在仓库根、执行 SHA `23d0a1f844d0f0f3601ccab94072ce86606dafe5`。私有证据根目录为 `D:/QQBotAudits/cards-groups-20260923`（下称审计目录）。

| 命令 | 结果与证据 |
| --- | --- |
| `uv run --locked pytest --junitxml=D:/QQBotAudits/cards-groups-20260923/full-23d0a1f.xml` | 2581 passed / 19 skipped / 0 failed/error，2600 项；同名 log/XML 保留 |
| 同次 JUnit 提取 `tests/test_r132_review_*.py` 子集 | 31 文件 / 262 passed，无失败、错误或跳过；原主审探针未修改 |
| `uv run --locked ruff check app tests alembic` | 通过 |
| `uv run --locked ruff format --check app tests alembic` | 357 文件通过 |
| `uv run --locked mypy app` | 123 源文件通过 |
| `uv run --locked ruff check scripts/initialize_group_actions.py scripts/image_allowlist_replay.py scripts/image_review_export.py` | 通过；新增初始化脚本格式检查亦通过 |

本机跳过不能全称 Windows 平台差异：13 项因生产 runtime 锁保护而跳过，4 项依赖符号链接权限，另有真实图片样本与可选巡检运行时各 1 项。CI 隔离环境覆盖本机锁保护跳过项。汇总收据 `validation-23d0a1f.json`。

精确源码 [CI run 35843307318](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35843307318) 最终 attempt 2 的 Ubuntu、Windows、clean runtime-deps 均成功。核验命令 `gh run view 35843307318 --json headSha,status,conclusion,attempt,jobs,url` 及对应 job 日志；Ubuntu `uv run pytest` 为 2594 passed / 6 skipped，Windows 为 2596 passed / 4 skipped。日志实际 checkout 为 PR 合成提交 `ce8f67a9cb7bc13d1250ab9f03ad361905c92015`，tree `10f9cdb1774fc96ad9de8a8baef4dc6ebdb36114` 与上述源码 HEAD 完全一致，不混称 checkout 为分支 SHA。

Windows attempt 1 曾因耗时过长被本任务取消；取回日志后确认仍在推进、没有断言失败，不能称卡死或全量成功。其日志原样保留 `ci-windows-attempt1-23d0a1f.log`；随后仅重跑 Windows，完整成功日志为 `ci-windows-attempt2-23d0a1f.log`。最终 API 收据为 `ci-23d0a1f.json`。

## 实际群授权与例外

执行 SHA 同上。实时身份和机器人自身角色核查见 `predeploy-readonly/receipt.json`、`all-large-group-permissions-20260923T093950Z/receipt.json` 及同目录采集命令。五个待开群机器人均为管理员。绵阳免费家教群 `628717263` 机器人为普通成员；负责人明确选择「不审核也不开启真实动作」。

先执行 `.venv/Scripts/python.exe -X utf8 -B D:/QQBotAudits/cards-groups-20260923/disable_mianyang.py --execute`，通过既有后台 CAS/审计路径把绵阳审核和动作都关闭，保留 owner/route。准备/提交收据和操作前数据库备份在 `disable-mianyang-01/`；调用实际运行时 getter 验证双关闭。

随后执行：

```powershell
.venv/Scripts/python.exe -X utf8 -B scripts/initialize_group_actions.py --group 822284640 --group 784172346 --group 594188592 --group 609631435 --group 245106525 --execute --evidence-dir D:/QQBotAudits/cards-groups-20260923 --authorized-by '负责人2026-09-23明确授权包含200人的也开；本次仅此五群，recall_only；绵阳群另按最新指令保持双关闭'
.venv/Scripts/python.exe -X utf8 -B D:/QQBotAudits/cards-groups-20260923/verify_groups.py D:/QQBotAudits/cards-groups-20260923/40e7d67098e44029b1c1b514927b132a/committed.json D:/QQBotAudits/cards-groups-20260923/groups-verified-23d0a1f.json
```

提交操作号 `b557bcfd8f5c44c4a62d62c6c105da5b`。settings/owner/route 各新增五行并精确复核，其他群、成员/词语白名单、急停保持不变。执行时创业群为 211 人，其余四群各 200 人；人数会随成员变化。

`2026-09-23T10:11:49Z` 复核：141 个目录群，72 个 >=200，71 个审核和动作均开启且运行时路由解析为 OneBot；唯一例外为绵阳双关闭，没有其他缺失。这是当时全量目录的配置与路由核查，不代表以后新群自动启用，也不代替逐条消息撤回成功证据。

## 生产加载与业务恢复

执行 SHA 仍为 `23d0a1f844d0f0f3601ccab94072ce86606dafe5`。私有操作在审计目录 `deploy-01/`：`deploy.py preflight`、提升权限运行 `deploy-services.ps1`（内部依次 quiet / backup / configure）、`deploy.py verify`。直接核验调用使用仓库 `.venv/Scripts/python.exe -X utf8 -B`；PowerShell 内部使用同一 Python 的 `-B` 与 `PYTHONUTF8=1`。授权、脚本哈希、前态、各阶段收据与服务状态均保留。

先等队列与在途动作清空，再正常停止 Runtime/Web，直接在 D 盘生成 `moderation-20260923T101208Z-usijtw2w.db`（SHA256 `2556a8d3dc1992099f54e9cf990487ca99c8275cb75a436672541e23e4acd37f`）。完整性、外键核验通过；revision 仍 `e1c7d4b8a902`，无迁移、无回滚、无历史重放。随后 Web/Runtime 启动，NSSM PID 从 31940/13160 变为 33776/12596；服务操作 UTC 10:12:05—10:12:14，OneBot 于 10:12:35 重连就绪。首次紧接启动的检查尚未 ready，稍后复核成功，不计作两次部署。

最终命令 `.venv/Scripts/python.exe -X utf8 -B D:/QQBotAudits/cards-groups-20260923/deploy-01/deploy.py verify`，`verified.json` UTC `10:15:56`：后台登录与健康接口均 HTTP 200，OneBot ready/online；备份之后自然接收 29 条均 DONE，29 条判定落盘，11 个动作意图均 SUCCEEDED。它们证明业务恢复，**不将普通消息动作冒称新版卡片实机验收**。没有向群发送测试消息。

已核对变更模块与提交源码字节一致；`.env`、提示词和部署前授权完成后的策略表摘要不变。提示词仍 `t204-v18`，急停仍 false，`recall_only` / image_hash shadow 保持；既有校园墙图片与两分钟窗口未改。

## 后续核验边界

独立只读命令（执行 SHA 同为 `23d0a1f`，仓库 Python 前缀同上）：

- `D:/QQBotAudits/cards-groups-20260923/postdeploy-independent/collect.py`：UTC 10:18 核对新进程、NSSM 工作目录/启动模块、源码字节、健康及群状态，结果通过。末级 Python Runtime/Web PID 为 37272/11936。受权限限制未读取子进程完整 CommandLine 或内存模块，加载结论来自启动配置、新进程、源码与自然业务的交叉核验；不冒称内存自检。
- `D:/QQBotAudits/cards-groups-20260923/natural-card-check/audit_natural_cards.py`：UTC 10:18:55 冻结 `36019 < shadow_id <= 36063`，共 44 条新记录；36019 是备份下界。唯一分享卡 SD36050 命中保护角色豁免，allow 且无精确关联动作；没有非保护新卡片结构命中。该 inbox 原载荷已按既有流程清空，不能再确认卡片子类。私有脱敏收据不是新增入库测试。

新增格式的自然非保护角色卡片命中与撤回回执仍需独立观察；未声称所有客户端格式都已覆盖，也没有追罚历史消息。普通二维码图片和微信小程序分享卡是不同消息类型。群主、管理员、成员白名单仍免撤回。

此记录后的交接同步提交只改文档；精确最终 HEAD CI 在推送后核验，不借用源码 run。接手命令：`git log -1 --format=%H`、`gh run list --branch windows-deploy-2026-09-10 --commit <实际HEAD> --json databaseId,headSha,status,conclusion,url`，再对对应 run 查三个 job；最终结果保存在审计目录 `ci-final-head.json`。不为把未知自身提交 SHA 写入文档而循环提交。
