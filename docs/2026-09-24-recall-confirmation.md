# 撤回确认与部署准备（RECALL-CONFIRM-20260924）

负责人先授权制作和全面检查，随后明确批准上线；生产迁移、备份和服务恢复结果见末节。根代理为唯一仓库写入者，辅助代理只读审查。此前案件跨页选择及“人工处理”补正随本批一并加载；未重放旧消息。

## 诊断边界

此前截图的一条群名片与修复部署前的旧判定相符；另一条合并转发已有一次撤回 API 成功回包，但旧代码丢弃所有 group_recall 通知，无法用该回包证明手机端实际消失。get_msg 的空 message 也不是可靠撤回证据。诊断原始材料只在 D:/QQBotAudits/card-incident-20260924；没有恢复全部历史载荷，不能断言第三方回调漏洞就是该次实际原因。

本机 NapCat 源文件 SHA256 eeed7d12ab4dba2adea6b85a49a85f842661adc9a597de92a4b406b597c0adf6：group_recall 基类在 63763 行用本机 Date.now() 生成秒级 time（80620、81125 一带未覆盖）；消息则在 73526 一带取 QQ msgTime。这两种时间源不能直接排序。本轮没有修改 NapCat/QQ 文件，也没有向群发送验证消息。

## 最终行为

- 保留 ActionIntent.status=SUCCEEDED / ActionLog.ok 的既有 **API 返回成功** 语义；新增 recall_confirmations，发请求前提交登记，QQ 通知单独保存，早到通知不被之后的 API 回包覆盖。
- 适配器明确携带接收账号。只有当前鉴权 WebSocket 的 group_recall，账号、群、发送者、消息和操作者全部匹配才确认；操作者必须等于机器人账号。字段严格校验，不猜昵称、消息键或跨账号映射。
- 本机请求到接收最多 300 秒；notice.time 不晚于本机接收，且不早于本次请求所在整秒。QQ 消息原时间只作审计。时钟回拨、不同步远端 NapCat、窗口外通知均保守留待核实。
- 仅存在一个候选才关联；包括已确认记录在内检查冲突，通知指纹唯一且更新幂等。历史未登记动作不追认，不自动补建确认。
- OneBot full 阶段在 API 返回后查询确认，未确认即终止该条后续禁言/警告；迟到通知只补证据、不续罚。当前生产仍 recall_only，不会因新版本启用其他动作。官方通道不改。
- FAILED/UNKNOWN 与已收到 QQ 通知可同时存在，分别展示；不覆盖原接口结果。管理员人工撤回不产生违规真值。
- 判定详情按快照意图 ID 加完整业务身份读实时动作/确认记录，避免冻结 JSON 丢失迟到状态；不把 ShadowDecision 的 onebot 去重键当数字 ActionIntent.message_id。日报/周报新增 confirmed / unconfirmed / untracked 计数，actions_ok 明示接口口径。
- 新确认表按既有判断保留期清理；原 ActionIntent 幂等历史不删除。空间巡检工具、二维码政策、校园墙窗口、群授权、通知开关和案件内容不变。

确认来自 QQ/NapCat 撤回通知，仍不是所有客户端视觉消失的证明。协议缺乏消息 generation，同秒完整身份短 ID 碰撞无法绝对区分；保留原 inbox/动作幂等保护，不承诺消除第三方所有异常。

## 数据、备份与待部署兼容

新增迁移 f3c8a9d12064，前驱 e1c7d4b8a902；只建新表，不回填历史成功。Web/Runtime 仍严格要求最新 schema。清理 CLI 仅对“本代码 head=f3c… / 旧库=e1c…”开放明确兼容路径，旧库无确认表时跳过该表；更老/未知库仍拒绝。只读日报旧库全部记 untracked。

新建备份配置使用代码 head；已有配置不自动修改。备份 CLI 在归档前要求代码 head 与配置 expected_revision 一致，避免新源码配旧库被标为可直接恢复。因此待部署期间的新定时备份会明确拒绝版本不匹配，不覆盖旧快照。部署时必须一并将现有 plain 配置 expected_revision 更新为 f3c8a9d12064；保留同一 state_dir 内的 config.restore-e1c7d4b8a902.json 供旧快照恢复，不改对象库、模式、凭据排除策略或计划时间。

## 提交与测试适配

- 应用/迁移提交：9bd0a995a0b19a7b764f058a3269a09a7860d411。
- 新回归：b71d1c724f40e36006d7832d661a7f31a69efae1。
- 完整动作旧测试前提补齐：dd9454fec5c79f98ae20d8a6c00883afe8e00ce8。test_action_feedback_veto、test_onebot_action_stage、test_onebot_actions 真实调用合成 notice 匹配，保留原纠错/并发/处罚断言，不 mock 掉确认门禁；另测无通知停止链条。
- 两处原主审迁移版本元数据适配：4d5656608a6c756773c9c9caee454ee0fdfd70bb。test_r132_review_image_hash_core 的 upgrade head 精确版本期望，以及 test_r132_review_unmigrated_startup_boundary 的错误文本 head，分别从 e1c7d4b8a902 改为 f3c8a9d12064；负控旧库 c9a1f4d27e30、退出码、列约束、downgrade baseline 和全部业务断言结构不变。
- 适配前原件可从 dd9454f 读取；文件头已登记。D:/QQBotAudits/recall-confirm-20260924/audit_review_heads.py 验证仅两个 ast.Constant.value 改变，归一化后 AST 完全一致；回执 review-head-ast.json，辅助审查独立复核一致。
- TDD 首个测试在旧代码明确失败：API ok / 无 notice 仍调用 mute、warn；补正后只调用 recall。第一轮全量15项失败是13项旧完整链前提与2项旧迁移 head 字面量；保留 full-source.xml，不将它作为通过依据。

## 验证与部署状态

最终源码测试执行 SHA 为 4d5656608a6c756773c9c9caee454ee0fdfd70bb，环境 PYTHONUTF8=1，TEMP/TMP 位于 D:/QQBotAudits/recall-confirm-20260924/test-temp。部署前只读生产预检显示旧 schema、原服务进程、OneBot ready、急停 false、recall_only、image_hash shadow；部署后的状态见末节。

本地最终验证通过，未代替生产 QQ 自然消息验收：

| 检查 | 实际结果 |
| --- | --- |
| `uv run --locked pytest --junitxml=D:/QQBotAudits/recall-confirm-20260924/full-final.xml` | 2682 passed / 19 skipped / 0 failed，289.24 秒；总计 2701 |
| 原主审探针（上述全量内） | 31 个文件，262 passed / 0 skipped |
| 新撤回确认及迁移回归（上述全量内） | 43 passed |
| `uv run --locked ruff check app tests alembic` | All checks passed |
| `uv run --locked ruff format --check app tests alembic` | 366 files already formatted |
| `uv run --locked mypy app` | 127 source files，无错误 |

本地回执为证据目录下 validation-source.json；全量 JUnit SHA256 为 caffeea621aba03f43a4bd8e79177ae52c50ecd8e6dbd6c968a1e3a9d344266e。19 项跳过须按原测试的平台/能力条件解读，不能声称全部 2701 项实际执行。合成库迁移、SQLite 备份恢复、通知重复/迟到/存储失败及全链安全门禁均包含在通过结果内。

部署前最终 HEAD `2fc0d55` 的 Linux、Windows、干净运行时依赖三个 CI job 全部成功；PR merge checkout 的 tree 与分支 tree 一致。核验回执在同一证据目录 ci-final.json；当时的准备方案在 deployment-plan.json。下述上线记录更新仅修改文档，不改变已测试的 app/tests/alembic 内容；本次文档提交自己的 CI 另行核验。

## 可审核部署顺序

1. 核对最终干净 HEAD、最新三个 CI job、剩余容量、当前服务及策略摘要、备份/清理任务未运行。当前预检不是未来执行时的状态。
2. 获负责人本批生产迁移/短暂重启授权后，选择队列排空窗口，暂停 Web/Runtime；期间消息可能无法补齐。
3. 在 D 盘生成并校验迁移前 SQLite 快照，保存现有备份配置及策略摘要；未经 verified-backup 检查不得执行迁移或启动新服务。
4. 用本批源码将库从 e1c7d4b8a902 升至 f3c8a9d12064，校验 revision、表结构、完整性、外键和旧业务数据；同一私有 state_dir 保存旧版恢复配置并原子更新当前备份 expected_revision。
5. 启动 Web/Runtime，检查新进程、healthz、OneBot ready、源码及策略摘要不变；后台跨页案件功能与动作详情只读核查。
6. 运行更新后的现有备份配置，在 D/E 新私有目录验证新快照恢复；核对旧配置仍适用于旧快照。不得再次堆积完整副本到 C 盘，不删除旧证据。
7. 只通过自然新消息核对 group_recall 关联。无新样本就记待观察，不发测试消息、不追撤历史、不自动重试。

回退需保持源码、数据库、备份配置三者一致；迁移后有新业务时不得直接覆盖回旧库。失败先停在当前步骤、保存证据，由负责人决定恢复或向前修复。本轮“准备部署”不等于生产迁移、重启或真实 QQ 撤回验收完成。

## 授权部署（2026-09-24）

负责人明确回复“现在就部署上线”。部署的应用及迁移版本为 `2fc0d55a03a76be0a935b6bfd975445424e63bbb`，提交前的 [CI 35951909027](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35951909027) 三 job 全部成功；逐 job checkout 的 tree 与该版本一致。部署脚本、用户授权、迁移前后及恢复收据在 `D:/QQBotAudits/recall-confirm-20260924/deploy-01`，目标和脚本 SHA 已固定，未重放历史消息。

维护窗口前确认 QQBotAutoCleanup 与 QQBotDailyBackup 均未运行，下一次计划时间分别为 09-25 04:00 与 04:30:30（北京时间）。队列排空后短暂停止 Runtime/Web。D 盘迁移前 SQLite 快照 `moderation-20260924T035851Z-g9o_7vhd.db` 的 SHA256 为 `fb80c84d1199142190dedcd22f707e9ef399ebaf02ca636d61641be3d439b670`，完整性、外键、旧版本及旧业务记录数均校验通过。数据库从 `e1c7d4b8a902` 升为 `f3c8a9d12064`，新增表初始为空；备份配置只将 expected_revision 同步到新版本，旧配置保存在原私有 state_dir 的 `config.restore-e1c7d4b8a902.json`。备份格式仍为不含凭据的 plain。

Web/Runtime 以新进程启动，健康页、后台登录页均 HTTP 200，OneBot ready/connected、队列空；`.env`、提示词规则、急停 false、recall_only、image_hash shadow、群授权及白名单摘要与迁移前相同。新配置实际完成 E 盘快照 `20260924T040312Z-136c36eb76b540ada0f161ec05712a67`，15,010 文件、缺失媒体 0；在 D 盘完整恢复并验证新数据库。旧配置用既有快照 `20260923T203004Z-f415e65b71fd4fb0bceb4699d200be9d` 在 D 盘完整恢复 14,440 文件，旧数据库完整性、外键与旧版本均通过。没有在 C 盘建立完整恢复副本，也没有删除这些验收证据。

部署后自然消息继续进入，尚未观察到匹配的 QQ `group_recall` 通知。`recall_confirmations` 截至 12:10 北京时间有 17 条待确认、0 条已确认；这不证明撤回失败，也不能算实机撤回验收通过。系统按设计不据 API 成功虚报真实撤回，不自动补罚或重放。此前跨页案件勾选与默认“人工处理”补正一并加载；未代管理员结案。Windows 整机故障恢复演练依原决定另排维护窗口。本轮后续文档提交不改应用/迁移/测试代码，最终文档 HEAD 的 CI 需单独核验。
