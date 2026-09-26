# 方案 A 生产上线 A2-DEPLOY-20260922

负责人在了解停服、迁移和回填流程后明确指令“方案 A 上线”，本轮据此执行生产维护。此前禁止生产迁移的状态被本次限定授权更新；通知、enforce、判断阈值、群授权、动作开关和原件删除均不在变更范围。Windows 整机故障演练仍待排期。

## 版本、命令与上线结果

维护前分支 `windows-deploy-2026-09-10` 为 `3f7f9376a2c2e4bd9c53eb3187ec0c62e2cce610`，已 pull 对齐且工作区干净。部署源码 **`53683d5998588f3b7490f4e73774df313b17b51d`** 来自 PR46，候选 CI run [35629685525](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35629685525) 实际检出 `32a11264d799540d2131942779ed17fc7fd07961`，Ubuntu/Windows/clean runtime-deps 全通过。完整源码测试与适配见 [候选验收](2026-09-22-authority-a2-validation.md)，本轮没有修改应用或测试源码，仅记录部署。

令 `<P>` 为生产仓库 `.venv/Scripts/python.exe`，`<O>` 为本机私有目录 `C:/Users/<local-user>/AppData/Local/QQBotDeploy/a2-20260922-01`。实际操作为 `<P> -B <O>/deploy.py prepare`，正常管理员权限运行 `pwsh -NoProfile -File <O>/deploy-services.ps1`，以及 `<P> -B <O>/verify-live.py`。包装脚本哈希、逐阶段命令、执行 SHA、退出码、日志哈希与脱敏结果见 [部署证据](evidence/authority-a2-deployment-20260922/summary.json)。原始数据库、JSON、原图、日志和计划仅留本机，未上传 Git。

先在真实在线一致性备份的独立副本执行 `python -B -m alembic upgrade head`、`scripts/backfill_image_decisions.py --db <copy> --dry-run/--apply`、`scripts/apply_review_decisions.py --db <copy> --export-only/--verify-authority`，并指向生产原件只读核验身份。备份恢复到另一个文件后，旧版本启动门禁通过；生产库与服务在这一步仍不变。

真实副本验证通过后，等待一次队列、PROCESSING 和未终结动作均为空的观察点，确认无旧审核 CLI 写入者；正常停止 QQBotRuntime 和 QQBotWeb。停服时再次 online backup 并校验，把库与 JSON 同时封存，再 `git merge --ff-only 53683d5998588f3b7490f4e73774df313b17b51d`。在生产库执行同样迁移、dry-run、apply、幂等复跑、export-only、verify-authority 和原图身份核验，完成后启动 Web/Runtime。未终止 QQ/NapCat，未强杀进程，未执行降级或生产库恢复。

## 已取得的生产证据

以下数字均来自上述部署 SHA、阶段命令及 summary 中的原始收据：

- revision 从 `d4b7c1e9a502` 升为 `e1c7d4b8a902`，权威完成标记为 1。
- 原 68 行全部旧字段保留，新增 1 行 disabled/rejected；生效名单仍 68 条。69 条决定落入同库权威，重复回填 `changed=0`；JSON 对账 `ok`，身份核验 allowed 68/rejected 1、mismatch 均 0。
- 停服前快照与迁移后启动前对比：其他业务表逐表数量/内容摘要一致，除权威标记外 system_settings 一致；`.env`、应用和通知配置、群配置、所有者/路由摘要不变。
- 停服备份 SHA256 为 `3e68bb45e39bfdc2e8f110e206901b5c82ce91ecbcc0c653600054da1d1c6f35`，旧 JSON 为 `7926f0bff94c0fed76b4126d0388b2b07a85dff0c4e4c9a81a38f760c6820dc6`。私有独立副本恢复读回 hash 一致，integrity_check=ok，foreign_key_check 无异常。审核证据复制后逐文件哈希与原清单一致。
- UTC 01:43:35.996 停 Runtime，01:43:37.128 停 Web；01:43:48.537 Web Running、01:43:50.188 Runtime Running。OneBot 于 01:44:06.263 重连。服务 Running 与业务 ready 分别核验，断线期间上游消息不保证补齐。
- UTC 01:45:44.976 的 `verify-live.py`：`/healthz` HTTP 200、OneBot ready/connected/online、队列 0；相对停服备份 rowid 上界，已自然新增 inbox 37 条且全 DONE、shadow 37 条、动作意图 4 条且全 SUCCEEDED。没有发送测试消息，也没有重放或补罚旧失败记录。该短时观察不等于全天稳定性或人工逐条核对 QQ 客户端动作。

一次临时观测脚本初稿把 inbox 当作有 `id` 列，发生只读查询错误；改用已核实存在的 SQLite rowid 上界后重跑通过。该错误未触及生产数据，不是迁移或服务失败；部署原始日志保留。

## 恢复边界与剩余事项

当前新库不能直接换回未适配的新旧 schema 不同的旧代码，启动门禁会拒绝。恢复流量前的预迁移备份恢复路线已在副本验证；恢复流量后不能无说明覆盖整库，否则会丢失新消息及动作记录。若以后回退，应先制定保留新增业务记录的同 revision 兼容方案；本轮没有执行生产回滚。

已完成本次实际备份、迁移、回填、部署、重连与有界业务恢复验证。真实整机故障、凭据/媒体恢复仍未演练；N03 历史副本源期限与原件处置、历史失败原件、真实群卡片原文验收仍未关闭。通知继续按负责人决定保持关闭，图片 hash 仍为原 shadow 模式。
