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
| `AI_*` | 默认关闭；需要远程审核时另行配置供应商、模型、预算和获批群范围 |
| `AI_PROMPT_RULES_FILE` | 保留与本版本配套的 `config/ai_prompt_rules.txt`，不能遗漏或拿旧版替换 |
| `NOTIFICATION_*` | 不因安装巡检自动开启通知，按公司批准范围另行配置 |

确认是空的新数据库后，部署人员执行建库迁移：

```powershell
uv run --no-sync alembic upgrade head
```

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

服务化由部署人员在前台验收后安排。**附带 `scripts/install-services-nssm.ps1` 当前会无条件安装/替换并启动 `QQBotWeb` 和 `QQBotRuntime`，不能直接作为 NapCat-only 的自动安装入口。** 必须先适配部署模式或由维护人员单独配置获批服务；这属于本候选版正式交付前待办，不能让缺少官方凭据的进程持续失败重启。已有同名服务时先核对，不能直接覆盖。NapCat 仍需要正常的交互登录会话；按实际 NapCat 版本配置启动方式，附带 BAT 仅是待填写模板。

## 操作人员：日常工作

- 群管理：核对群号、审核开关、动作开关和唯一执行出口；真实动作按公司的逐群批准流程开启。
- 案件：核对消息证据和成员身份，再按后台预览与确认流程处理。API 返回成功与 QQ 撤回确认记录分别查看，不把未知状态当作完成。
- 白名单：成员白名单与关键词白名单作用不同，变更须按本版本页面说明和公司授权执行。
- 急停：发生账号错配、异常动作或不确定状态时由授权人员使用急停，再联系维护人员核查；不要反复重放历史动作。
- 专项成员巡检：使用独立桌面工具。它的选群不等于给主服务授权，扫描结果不会自动生成案件、修改白名单或触发处罚。

如需多名员工从其他电脑使用后台，网络暴露、身份权限与访问范围须另行部署和验收。当前巡检仍在 NapCat 所在电脑操作，不能据此承诺同样的远程能力。

## 与巡检配合的边界

巡检发现空间限制时，操作人员先人工复核，再按公司现有处理流程决定是否处置；不能据此自动认定永久封号。主服务的群授权、保护角色、成员白名单及人工审批规则继续独立生效。主服务运行不需要打开巡检窗口；已保存的巡检结果也不会改变主服务历史判定。

数据保存与恢复见 [维护清单](acceptance-maintenance.md)，尤其注意普通 ZIP 目录尚不能直接沿用依赖 Git 的每日备份流程。
