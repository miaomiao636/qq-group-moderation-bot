# 切换 OFFICIAL 模式操作手册

本手册描述从 SHADOW（仅记录）切换到 OFFICIAL（真实撤回/禁言/警告）的完整流程。
所有开关均为 **fail-closed**：任一缺失，违规消息只记一条 SKIPPED 意图，不执行任何动作。

## 前置条件（不可跳过）

1. T-303 影子验证主审验收通过（24h 数据 + 断线演练证据齐备）
2. T-307 NapCat 动作 Adapter 主审验收通过
3. W3 隔离测试群实测：真的撤回一条、真的禁言 1 小时 / 24 小时、警告回复真的发出
4. 已知风险并接受：OFFICIAL 模式会对真实群成员执行处罚

**未完成前置条件前，下面的开关即使全开也不会执行动作**（`ONEBOT_ACTIONS_ENABLED` 默认关闭，
且代码同步/服务重启/NapCat 重连均不会自动开启真实处罚）。

## 三层开关（缺一不可）

```
第1层：全局配置（.env + 重启服务）
├─ APP_ENV=prod
├─ ACTION_MODE=OFFICIAL          ← 主开关：SHADOW → OFFICIAL
├─ EMERGENCY_STOP=false
└─ ONEBOT_ACTIONS_ENABLED=true   ← T-307 独立第二道开关（默认 false）

第2层：每群路由（group_provider_routes 表）
└─ 消息入口 provider → 动作出口 provider 的同通道路由
    （面板勾"动作"保存时自动补齐，见"动作出口"列；也可手动 upsert_group_route）

第3层：每群面板开关（管理后台 → 群管理）
└─ 勾选"动作"并保存
```

## 操作步骤

### 1. 修改 `.env`

```ini
APP_ENV=prod
ACTION_MODE=OFFICIAL
EMERGENCY_STOP=false
ONEBOT_ACTIONS_ENABLED=true   # T-307 第二道开关
```

> 其余生产配置（`ADMIN_PASSWORD` / `QQ_APP_ID` / `QQ_APP_SECRET` / `ONEBOT_WS_ENABLED` /
> `ONEBOT_ACCESS_TOKEN`）必须已正确设置。`ACTION_MODE=OFFICIAL` 启动校验会强制要求这些。

### 2. 重启服务

```powershell
# 管理员 PowerShell
sc.exe stop QQBotWeb; sc.exe stop QQBotRuntime
Start-Sleep 3
sc.exe start QQBotWeb; sc.exe start QQBotRuntime
```

启动时配置校验 fail-closed：缺任何一项直接报错拒启（不会以半开状态运行）。

### 3. 面板开启目标群动作

管理后台 → 群管理 → 目标群勾选「动作」→ 保存。

- 保存时自动按该群已见消息来源补齐同通道路由（`动作出口`列显示 `onebot→onebot`）
- **建议先只开一个低风险隔离测试群**，观察至少 24 小时再开其他群
- 关闭动作不会删除路由（路由是拓扑信息，动作开关由 `action_enabled` 守门）

### 4. 验证

- 触发一条高置信违规（测试群发广告词），观察：
  - 消息被撤回
  - 发送者被禁言（首次 1 小时，二次 24 小时）
  - 群内出现警告回复
- 管理后台 → 案件/动作日志可审计每次执行结果
- `action_intents` 表出现 `SUCCEEDED` 状态记录

## 紧急回退

### 急停（不重启，立即生效）

`.env` 设 `EMERGENCY_STOP=true` 后重启服务，或通过管理后台急停开关（如有）。
急停后所有群所有动作立即 SKIPPED，审核继续记录。

> 注意：`ACTION_MODE=OFFICIAL` 与 `EMERGENCY_STOP=true` 在配置层互斥——
> 启动校验会拒绝同时持有二者。急停的正确用法是先 `EMERGENCY_STOP=true` 再把
> `ACTION_MODE` 改回 `SHADOW` 重启，避免配置冲突。

### 完全回退到影子

```ini
ACTION_MODE=SHADOW
ONEBOT_ACTIONS_ENABLED=false
```

重启服务。已执行的撤回/禁言不可逆，但新消息不再产生任何动作。

## 按群临时关闭

不改动全局配置，仅对个别群停止动作：

管理后台 → 群管理 → 该群取消勾选「动作」→ 保存。

该群立即回到只记录不执行；其他群不受影响。

## 安全保证（T-307 实现）

- **数字 ID 强制校验**：非数字 `group_id/user_id/message_id` 直接 FAILED，绝不伪装或猜测
- **发送前未就绪 = FAILED**（确定未发出请求）；**发送后超时/断线 = UNKNOWN**（结果不确定，冻结禁止自动重放）
- **保护角色**：群主/管理员/白名单的违规只记录不处罚
- **幂等**：同一动作意图最多成功一次，重复事件不重复处罚
- **无踢人**：Adapter 不含任何踢人逻辑（AST 守护测试保证）；踢人必须人工审批（T-304）
- **默认关闭**：`ONEBOT_ACTIONS_ENABLED` 默认 false，代码同步/重启/NapCat 重连均不自动开启
