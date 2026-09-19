# r132 第十批送审材料（受审 SHA `dbd80a5`）

日期：2026-09-19。**受审 SHA**：`dbd80a5`（工作区干净）。**基线**：`8299ce8`（您第八批复核的受审版本）。
本批 = **R9-01～R9-10 + D01 整改** + **负责人授权的 B 方案生产变更**。

---

## 一、证据塔（可复算）

| 检查 | 结果 |
| --- | --- |
| **入库主审探针合计** | **151 passed / 0 failed**（113 三批 + **本轮 38 项**） |
| 本轮 38 项 | **先原样入库**（入库时 25 failed / 13 passed，Windows 本机）→ 整改后 **38/38 通过** |
| 仓库全量 | **1786 用例 / 0 failed / 0 error / 15 skipped** |
| `ruff check` + `format --check` | **292 文件**通过 |
| `mypy app` | **89 源文件**无问题 |
| **同 SHA CI（`dbd80a5`）** | [run 35451072498](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35451072498) —— `Lint, Type-check & Test (ubuntu-latest)` `jobId=105918202246`、`Lint, Type-check & Test (windows-latest)` `jobId=105918202333`、`Runtime deps regression (clean install)` `jobId=105918202302`，**三 job 全部 success** |
| 本轮新增回归 | `tests/test_r132_review_group_survey_account_binding.py`（平台无关地钉住 R9-03 账号绑定契约） |
| 本文档所在提交 `e3f321c` 的 CI | [run 35451698502](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35451698502) —— Windows `jobId=105919868577`、Ubuntu `jobId=105919868783`、clean runtime-deps `jobId=105919868729`，**三 job 全部 success** |

---

## 二、R9 逐项整改映射

| 主审编号 | 根因（我独立复现） | 修法 | 证据 |
| --- | --- | --- | --- |
| **R9-01 / P1** 群号代替 provider-qualified 身份 | 普查/前态/UPDATE/生成回滚全按群号 | 采集、前态、`UPDATE`、审计、**回滚全部 `(provider, 群)` 限定**；同号另一 provider 行**不受影响** | 5 项 groups 探针转绿 |
| **R9-02 / P1** 先提交后审计 | `commit()` 在审计写盘之前 | **审计先落盘再提交**；写失败 → 事务回滚、不留"已开启无证据"；**唯一 op id** + plan/applied 两份**只增不改**；输出区分未提交 / 已提交但导出失败（exit 3）/ 完整成功 | 2 项转绿 + 正控（备份含前态、重复执行不增行）保持 |
| **R9-03 / P1** 未绑定账号与阶段 | 首个响应即返回；审计 `stage` 是写死常量 | 普查**只用 `onebot11_<self_id>.json`**，取不到即拒绝；同时写**结构化 JSON 快照**（执行侧不再解析可疑群名）；阶段读 环境变量→`.env`，不是 `recall_only` 就拒绝；审计写 `stage_declared` + **`stage_runtime_proven=false`** | 3 项转绿（含平台适配，见第三节） |
| **R9-09 / P2** 虚报"已开启" | 旧普查"是"覆盖 DB 前态；UPDATE 不查 rowcount；新行只写 settings | 前态**只认 DB 现值**；`UPDATE` 校验 `rowcount`（漂移即报错）；**新建行要求路由就绪**（与运行时同语义），未就绪 → **阻塞、不开**、**绝不自动建跨通道映射**；"配置位"与"执行就绪"分列 | 3 项转绿 |
| **R9-04 / P2** 批准不绑字节身份 | 只解析编号→文件名 | 按清单**完整 SHA-256 + dHash** 校验磁盘字节（同一份字节算哈希，不再二次读取）；同名换图 → **拒绝批准** | 1 项转绿 |
| **R9-07 / P2** 漏处理仍报成功 | 两个解析器只认两位编号 | 编号改**正整数**（第 100 张起不再漏渲染/漏落库）；原图缺失、编号未知、结论未定、**清单里有图没给结论** → `APPLY_FAILED` + 非零 + **零写入** | 3 项转绿 |
| **R9-05 / P2** 最新决定语义 | `INSERT OR IGNORE` 让"第三次放行"失效；普通导入不读拒绝 | 区分**普通导入**（尊重拒绝）与**显式重新批准**（`record_approval` + 重新启用既有行 + 保留撤回历史） | 3 项转绿（含正控"撤回会真正关闭既有行"） |
| **R9-06 / P2** 快照损坏/并发/漏读 | 损坏当空集后覆盖；无锁丢记录；缺表回放不读快照 | **锁内读改写 + 原子替换**；损坏 → 抛错、**保留原件**；区分 `missing`/`corrupt`；导入/回放/导出**共用同一份拒绝记录** | 3 项转绿 |
| **R9-08 / P2** 阈值读了不存在的属性 | `getattr(ai_service, "direct_threshold", 0.90)`（真实字段是 `primary_direct_threshold`） | 新增 `AIReviewService.policy_snapshot()`，**持久化与 shadow 判据同源**；另写 `review_policy_source`（`service` / `assumed_defaults`）；`:481` 同源改造 | 2 项（0.80/0.95）转绿 + 默认正控保持 |
| **R9-10 / P2** shadow 汇总口径 | 全库计数写成"窗口内总数"；丢 `frame_scope`；称候选"会进入 enforce 放行" | 如实标 **"全库留存累计"**（或 `--since/--until` 真窗口，所有计数共用范围）；**unavailable 原因分布单列**；**首帧逐行 + 摘要披露**；删除 enforce 承诺表述 | 3 项转绿（含正控：异常观察保留并计入 unavailable） |
| **D01 / P3** 文案与实现不一致 | 我上稿总称"113 项全部原样、未改断言"；`window_stats` 旧文案与全局保留实现矛盾 | 改口径为 **11/14 AST 一致 + 3 处差异逐处明示**；`action_scope` 改 `global_all_providers_retained`，删掉与实现矛盾的旧描述 | 文案核对 |

---

## 三、一处**平台适配**（如实登记为第 3 处探针差异）

您第八轮 groups 探针里的注入条件是 `str(self) == "D:/QQ/config"`；而 **Windows 上 `str(pathlib.Path("D:/QQ/config"))` 是 `D:\\QQ\\config`（反斜杠）**，
→ 假配置永远注入不进去 → 该探针在 **Windows 上因错误原因失败**（首轮 CI `813cade` 的 Windows job 即因此变红）。

处理：**只把注入条件改成 `str(pathlib.Path(survey.ONEBOT_CONFIG_DIR))`（平台无关、语义等价）**，
**断言与意图逐字未改**，并在文件头与本节登记；另加平台无关回归 `tests/test_r132_review_group_survey_account_binding.py`。
因此 AST 口径为 **11/14 一致**，3 处差异 = ①scope 测试（您已接受的方案 2 改断言）②mode 测试（输入配置隔离）
③本处 Windows 注入条件适配。

---

## 四、运行态：本批**唯一**生产变更是你（负责人）授权的 B 方案

> 上批我写"真实动作群 13 → 67"是**过度陈述**：当时 67 群中只有 **13** 群有归属/路由、真正可执行，
> 另 54 群 `resolve_action_provider` 返回 `None`（fail-closed，**不会外发动作**）。此点已更正并留档。

| 项 | 内容 |
| --- | --- |
| 授权 | 负责人 2026-09-19 明确选择 **B**：「补齐 owner/route 让它们真正生效」（D-033） |
| 工具 | 新增 `scripts/authorize_group_routes.py`：provider 限定、`--authorized-by` 必填留档、dry-run 预览、一致性备份、**审计先落盘**、提交前逐群复核、附逐行回滚 SQL |
| 执行 | op_id `658e9770b7f7`：新增**归属 54 行 + 路由 54 行**；备份 `data/backups/moderation-20260919T145306Z-923oh8zi.db`；审计 `…/authorize-groups-…-658e9770b7f7.plan.json` + `.applied.json` |
| 复核（**运行时真实函数**） | `resolve_action_provider(session, 'onebot', 群)` → **67/67 返回 `onebot`**（此前 13/67） |
| 无副作用 | `provider_group_settings` 仍**只有 onebot**（67 开 / 3 关）；其它 provider 行数 0；**无需重启**（路由在每次动作前从库里读） |
| 不变量 | 阶段仍 **`recall_only`**（只撤回）；`IMAGE_HASH_MODE=shadow`；**enforce 未实现** |

### 图片批准的身份关联（您要求的第 4 步，已完成）

新增 `scripts/verify_approved_identity.py`（只读、可复算、失败非零），逐条连接
**生效名单 → 批次清单行 → 文件名/完整 SHA-256/dHash → 磁盘实际字节 → `DECISIONS.json` 人工结论**：

```
IDENTITY_OK allowed=68 (mismatch 0) rejected=1 (mismatch 0) snapshot_state=ok
→ docs/evidence/image-review/identity-20260919T152313Z.md / .json
```

- **68 条生效哈希全部可追溯到"您所审的那张图"**：文件实际字节的 SHA-256 与清单一致、dHash 与生效条目一致、结论为放行；
- **1 条拒绝**（`e9acacc888c8d0a2`）同样可追溯到一条"撤回"结论的清单行，快照读取状态 `ok`（非缺失/非损坏）。

### shadow 观察现状（口径已校准）

`shadow-20260919T152319Z.md`：**观察 223 条、命中（matched）5 条、would_allow 0 条、unavailable 0 条**；
报告首行显式声明"全库留存累计（未按时间/账号/版本分离）→ **不得**用作同窗覆盖率分母"。

---

## 五、仍未证明（照旧不掩盖）

1. A09 前 6 秒（17:01:22→17:01:28）逐条版本归属；
2. 历史授权快照未绑定（越界核查只与导出时刻对比 → `NOT_PROVEN`）；
3. 导出多查询无显式一致性读事务，行级原件待一致性快照重出；
4. **Windows 回滚全链未实机演练**（B 的回滚 SQL 已生成但未在生产演练）；
5. 一次未复现的失败（原因未定，仍列为待钉死）；
6. 图片白名单的 **enforce 最终批准与阈值校准**（enforce 未实现）；
7. B 的**实际执行效果**：67 群可路由 ≠ 已产生真实动作；需下一批用固定 UTC 窗口的动作统计原件验证。

---

## 六、请主审重点复核

1. **R9-02 的审计顺序**：我改成"审计文件先落盘 → 再 commit；结果文件导出失败 → exit 3 并保留 op_id"，是否满足您对"未提交/已提交但导出失败/完整成功"三分的要求；
2. **新行路由就绪门槛**：我把"新建 settings 行但路由未就绪"做成**阻塞不开 + 显式列出**（而不是自动建路由），是否符合您"不为跑绿自动扩大授权"的口径；
3. **B 的执行边界**：新增 54 行归属/路由**只针对 onebot**，同群若存在别的 provider 归属/路由则跳过并报告（本次实测 0 冲突）——请确认这种"配置位不变、只补路由"的做法不构成越权；
4. **R9-08 的 `review_policy_source`**：服务未暴露阈值时我记 `assumed_defaults`（等价于判据函数里文档化的默认值）并在报告里标注来源，而不是留空 `unknown`——请确认这个折中（留空会让您上一轮已接受的"离线与在线一致"探针失效）；
5. **第三处探针差异**（Windows 注入条件适配）是否可接受，以及是否需要我把新回归 `test_r132_review_group_survey_account_binding.py` 也交您审核。

---

## 七、可直接转发的回评

> 第八批 R9-01～R9-10 + D01 已全部整改，推送至 **`dbd80a5`**（基线 `8299ce8`）。
> **您本轮 38 项探针先原样入库（入库时 25 failed），整改后 38/38 通过**；三批入库探针合计 **151/0 failed**；
> 仓库全量 **1786 / 0 failed / 0 error / 15 skipped**；ruff（292 文件）+ mypy（89 文件）通过；
> **同 SHA CI** [run 35451072498](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35451072498) 三 job 全 success
> （Ubuntu `105918202246`、Windows `105918202333`、clean runtime-deps `105918202302`）。
> 映射：R9-01 → 全链路 `(provider, 群)` 限定；R9-02 → **审计先落盘再提交** + 唯一 op id + 三分状态；
> R9-03 → 只认 `onebot11_<self_id>.json` + 结构化快照 + 阶段须绑定 `recall_only`（审计写 `stage_runtime_proven=false`）；
> R9-09 → 前态只认 DB、校验 rowcount、新建行**路由未就绪即阻塞不开**、配置位与执行就绪分列；
> R9-04/07 → 批准绑定**完整 SHA-256+dHash**、缺失/未定/漏处理均非零且零写入、编号支持正整数；
> R9-05 → 普通导入尊重拒绝、**显式重新批准**才可重新启用（保留历史）；R9-06 → 锁内读改写+原子替换、损坏不覆盖、三工具共用同一份拒绝记录；
> R9-08 → 新增 `policy_snapshot()`，持久化与 shadow 判据同源读**真实字段**（0.80/0.95 不再被写成 0.90），并标 `review_policy_source`；
> R9-10 → shadow 报告如实标"全库留存累计"、unavailable 原因单列、**首帧逐行披露**、删除 enforce 承诺表述；D01 → 文档口径改 11/14 + 3 处差异。
> **一处需您知悉的探针适配**：groups 探针的注入条件 `str(self) == "D:/QQ/config"` 在 Windows 上是反斜杠形式、
> 注入失效导致**因错误原因失败**；我只把注入条件改为平台无关（**断言与意图逐字未改**）并登记为第 3 处差异，
> 另加平台无关回归 `test_r132_review_group_survey_account_binding.py`。
> **运行态（本批唯一生产变更）**：负责人明确选择 B，已用 `authorize_group_routes.py` 为 54 个"配置位已开但不可路由"的群
> 补齐 54 行归属 + 54 行路由（op_id `658e9770b7f7`，变更前一致性备份、审计先落盘、提交前逐群复核、逐行回滚 SQL）；
> 以**运行时真实函数**复核 **67/67 群 `resolve_action_provider→onebot`**；无跨 provider 影响、无需重启；
> 阶段仍 `recall_only`（只撤回）、`IMAGE_HASH_MODE=shadow`、**enforce 未实现**。
> **您要求的图片身份关联已出**：`scripts/verify_approved_identity.py` 逐条连接"生效名单 → 清单行 → 文件名/SHA-256/dHash → 磁盘字节 → 结论"，
> 结果 `IDENTITY_OK allowed=68 (mismatch 0) rejected=1 (mismatch 0) snapshot_state=ok`
> （`docs/evidence/image-review/identity-20260919T152313Z.md`）。
> shadow 现状：观察 223 / 命中 5 / would_allow 0 / unavailable 0（口径已校准）。
> 仍未证明：A09 前 6 秒、历史授权快照（`NOT_PROVEN`）、一致性读事务、Windows 回滚实机演练、未复现失败、
> enforce 批准与阈值校准、**B 的实际执行效果**（可路由 ≠ 已产生真实动作，需下一批固定窗口动作原件）。
