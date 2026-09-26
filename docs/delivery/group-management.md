# 群管理操作手册

配套入口：[交付说明](../../DELIVERY.md)。专项成员检查见 [空间巡检手册](space-inspector.md)；验收、备份与升级见 [维护清单](acceptance-maintenance.md)。本手册面向接收方的新环境，不是原生产环境的迁移或重启授权。

## 部署人员：准备新环境

采用公司指定的 Windows 专用电脑、QQ 账号及受控管理员账号。安装 Python 3.12（含 Tk）、uv、QQ/NapCat；巡检还需要 Microsoft Edge。服务化需要另外准备 NSSM。二进制依赖由部署人员从可信来源安装并记录版本，本文件包不附带第三方安装程序。

将源码解压到长期保留的位置，例如 `D:\CompanyQQTools`。后续服务和巡检快捷方式引用此位置，不能装好后删除或移动。确认目录内没有接收方已有数据库、`.env` 或服务实例，再按以下**全新安装**流程配置：

```powershell
Set-Location '<项目目录>'
uv sync --locked --no-dev
Copy-Item -LiteralPath .env.example -Destination .env
```

若 `.env` 已存在，不运行复制覆盖；由部署人员核对现有配置。真实密码、令牌和密钥只填在接收方本机，不写入手册、Git、截图或交付包。

| 配置 | 新环境填写原则 |
| --- | --- |
| `APP_ENV` | 正式服务用 `prod`，同时配置符合要求的管理员强口令 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 接收方自己设置，不使用原部署凭据 |
| `WEB_HOST` / `WEB_PORT` | 默认只监听本机，后台端口按部署规划填写 |
| `ONEBOT_SELF_ID` | 公司指定、当前登录 NapCat 的机器人 QQ |
| `ONEBOT_WS_ENABLED` / `ONEBOT_ACCESS_TOKEN` | 开启消息接入，令牌与 NapCat WebSocket 客户端一致 |
| `ACTION_MODE` / `ONEBOT_ACTIONS_ENABLED` | 首次验收保持 `SHADOW` / `false`，先验证只记录链路 |
| `ONEBOT_ACTION_STAGE` | 固定为 `recall_only`；自动审核只撤回，不启用旧版 `full` 禁言/群内警告档位 |
| `AI_*` | 默认关闭；需要远程审核时另行配置供应商、模型、预算和获批群范围 |
| `AI_PROMPT_RULES_FILE` | 保留与本版本配套的 `config/ai_prompt_rules.txt`，不能遗漏或拿旧版替换 |
| `NOTIFICATION_*` | 不因安装巡检自动开启通知，按公司批准范围另行配置 |

确认是空的新数据库后，部署人员执行建库迁移：

```powershell
uv run --no-sync alembic upgrade head
```

建表后，**仅对此处刚创建且没有业务数据的空库**，初始化图片判定记录。先预览，结果必须为 `rows=0`、`changed=0`；若有旧记录或报错，停止并交维护人员评估，不能套用新安装流程：

```powershell
uv run --no-sync python -c "from pathlib import Path; from scripts.image_decision_authority import backfill; print(backfill(Path('data/moderation.db')))"
uv run --no-sync python -c "import sys; from pathlib import Path; from scripts.image_decision_authority import backfill; p=Path('data/moderation.db'); plan=backfill(p); sys.exit('STOP: database is not empty') if plan['rows'] else print(backfill(p, apply=True))"
```

这是现有判定记录版本的初始化，不导入名单、不启用动作。缺少该步骤时备份会拒绝尚未初始化的记录；不能通过删检查或手写数据库标记解决。如果实际数据库位置不是默认的 `data/moderation.db`，先统一与备份支持范围核对，不直接复制这组路径。

已有生产库不套用以上命令；升级必须先备份、核对迁移兼容并安排维护窗口。巡检的安装步骤本身不需要主数据库迁移。

## NapCat 的主服务连接

在 NapCat 中配置 WebSocket **客户端**，连接 `ws://127.0.0.1:<WEB_PORT>/onebot/ws`，令牌与 `ONEBOT_ACCESS_TOKEN` 一致，消息格式使用 Array。该连接负责主服务消息接入及已有授权动作链路。

巡检还需要另一项本机 HTTP **服务端**配置，步骤见 [巡检手册](space-inspector.md#部署人员首次配置)。不要覆盖或删除已有 WebSocket 连接来配置巡检；HTTP 的访问令牌由巡检从本机配置读取，不能认为 WebSocket 已连上就代表巡检可用了。

仅使用 NapCat 时，在项目目录启动主应用即可，它同时启动后台与 OneBot 消息处理：

```powershell
uv run --no-sync python -m app
```

访问本机 `/healthz` 核对健康状态，在 `/admin` 登录后台，确认消息进入影子记录、当前机器人身份正确，且真实动作保持关闭。

`python -m app.runtime` 是可选 QQ 官方机器人运行器，需要另外配置 `QQ_APP_ID` / `QQ_APP_SECRET`，不是 NapCat-only 部署的必需步骤。

服务化由部署人员在前台验收后安排。安装脚本只适用于**新安装**，默认模式 `NapCat`，只安装 `QQBotWeb`。先在 PowerShell 预览（不会创建目录、读取私有配置或操作服务）：

```powershell
.\scripts\install-services-nssm.ps1 -ProjectDir '<项目目录>' -NssmPath '<NSSM完整路径>'
```

确认计划后，在管理员 PowerShell 执行同一命令并加 `-Apply`。脚本检查项目配置为 `prod`、管理员口令有效、OneBot 身份/连接已配置，以及 `SHADOW` / 真实动作关闭；检查失败不会安装。已有任何选中的同名服务即拒绝，不自动停止、删除或覆盖。只有确实使用官方机器人并配置官方凭据时才加 `-Mode WithOfficial`，此时另装 `QQBotRuntime`。

若 Windows 提示“禁止运行脚本”，先由公司 IT 核对包哈希与脚本、按公司的脚本签名/执行策略运行。该提示发生在安装程序执行之前，不是服务已经安装失败；不要为安装长期关闭整机执行限制。

`-Apply` 配置开机启动，但默认不立即启动。需要本次启动时追加 `-StartServices`，必须在已批准的安装流程中使用；勿在本机现有生产服务上重跑。可以通过 `-ServicePrefix` 为隔离的新实例选不同名字，但端口、数据和账号隔离仍由部署人员确认。中途失败可能留下部分新服务，应核对安装状态后单独处置；脚本不会自动删除它们。

NSSM 将失败交给 Windows 服务管理器：第一次失败 5 秒后重启，第二次 15 秒，随后停止；连续 24 小时无失败后重置计数。安装器回读配置，异常则停止安装。**这不是故障恢复实演**；非崩溃失败恢复标志按 Windows 文档在下次系统启动生效，须在公司验收重启和故障恢复、监控告警与人工接手。[Windows 恢复机制](https://learn.microsoft.com/en-us/windows/win32/api/winsvc/ns-winsvc-service_failure_actionsw)、[标志生效范围](https://learn.microsoft.com/en-us/windows/win32/api/winsvc/ns-winsvc-service_failure_actions_flag)。脚本不配置公司外部告警，连续失败由维护人员检查服务和日志。

NapCat 仍需要正常的交互登录会话；按实际版本配置启动方式，附带 BAT 仅是待填写模板。

## 操作人员：日常工作

- 群管理：核对群号、审核开关、动作开关和唯一执行出口；按公司的逐群批准流程开启仅撤回，不自动禁言、群内警告或踢人。近 30 天第二次有效违规及人工结案后再犯会建立待审案，成员处置仍由人工决定。
- 案件：核对消息证据和成员身份，再按后台预览与确认流程处理。API 返回成功与 QQ 撤回确认记录分别查看，不把未知状态当作完成。
- 白名单：成员白名单与关键词白名单作用不同，变更须按本版本页面说明和公司授权执行。
- 急停：发生账号错配、异常动作或不确定状态时由授权人员使用急停，再联系维护人员核查；不要反复重放历史动作。
- 专项成员巡检：使用独立桌面工具。它的选群不等于给主服务授权，扫描结果不会自动生成案件、修改白名单或触发处罚。

如需多名员工从其他电脑使用后台，网络暴露、身份权限与访问范围须另行部署和验收。当前巡检仍在 NapCat 所在电脑操作，不能据此承诺同样的远程能力。

## 与巡检配合的边界

巡检发现空间限制时，操作人员先人工复核，再按公司现有处理流程决定是否处置；不能据此自动认定永久封号。主服务的群授权、保护角色、成员白名单及人工审批规则继续独立生效。主服务运行不需要打开巡检窗口；已保存的巡检结果也不会改变主服务历史判定。

数据保存与恢复见 [维护清单](acceptance-maintenance.md)：普通 ZIP 使用固定清单哈希的 `bundle` 模式；Git 部署使用 `git` 模式，两者不自动降级。
