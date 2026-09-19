# r132 第十一批送审材料（受审 SHA `194eb0b`）

日期：2026-09-20。**受审 SHA**：`194eb0b`（工作区干净）。**基线**：`dbd80a5`（您第九批复核的受审版本）。

本批 = 您第九批复核（R9-03-R / R9-09-R / B01 / R9-04-R~R9-07-R / R9-05-R~R9-06-R / R9-08-R / R9-10-R / N-IDENTITY）的**全部整改**，**未夹带新功能、未做新的生产变更**。

---

## 一、证据塔

| 检查 | 结果 |
| --- | --- |
| **您本轮 42 项探针** | **先原样入库**（入库时本机可复现 **33 failed / 9 passed**）→ 整改后 **42 passed / 0 failed** |
| 全部入库主审探针（8 批） | **196 passed / 0 failed** |
| 仓库全量（本机） | **1828 用例 / 0 failed / 0 error / 15 skipped** |
| `ruff check` + `format --check`（270+ 文件）、`mypy app`（89 文件） | 全绿 |
| **同 SHA CI（`194eb0b`）** | [run 35454483168](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35454483168)：`ubuntu-latest` `jobId=105927237807` **success**、`clean runtime-deps` `jobId=105927237926` **success**；`windows-latest` `jobId=105927237916` 首跑 **failure**（唯一失败 = `tests/test_migration_notifications.py::test_notification_upgrade_constraints_and_downgrade`：`alembic upgrade` 子进程 **30s 超时**；该 job 本次耗时 13:21，逐条结果 `1 failed / 1824 passed / 3 skipped`；**与本次改动无关**——本批未触碰 alembic/迁移，本机同一测试通过），**已触发失败 job 重跑**，结论见 PR 页面或后续提交 |
| 生产身份关联（**重写后的严格工具**） | `IDENTITY_OK allowed=68 (mismatch 0) rejected=1 (mismatch 0) snapshot_state=ok` |

---

## 二、R9 逐项整改映射

| 编号 | 我方实现 |
| --- | --- |
| **R9-03-R**（顶层旁路 + 快照双读 + 未绑账号/provider） | ①`_groups()` **不再降级**到未核账号的 WebUI（拒绝即拒绝）；②两工具统一 `load_snapshot()` **一次读取**（元数据与群行同源，`--survey` 绑内容）；③**强校验** `self_id` 与 `provider`，缺失/不匹配一律拒绝；④兼容路径：仅当调用方**注入**了自己的名单（无快照可绑）才允许执行，且审计如实写 `source=injected_loader`（**NOT_PROVEN**，不冒充"已绑定快照"） |
| **R9-09-R**（只查 rowcount 不能防撤权/ABA） | 计划携带 **provider-qualified 行版本**；写事务内 `where provider=? and 群=? and action_enabled=0 and version=?` **CAS 复核**，撤权/删除/ABA/资格变化 → 拒绝旧计划；authorize 在**任何写入之前**逐目标重跑资格与冲突复核；`--threshold` 与快照**真正参与选择**（不在快照内或人数不达阈值 → 列阻塞） |
| **B01**（共享 owner 影响官方隐式路由 + 并发冲突） | 归属复核新增**官方隐式默认分支**：该群存在**其它 provider 的已启用 settings 行** → 冲突阻塞（`resolve_action_provider` 在"无 owner/route"时对 `qq_official` 仍返回 `qq_official`，插入 OneBot owner 会把它变成 `None`）；并发插入其它 provider 路由 → 写入前复核发现 → **先阻塞，不先写 owner** |
| **R9-04-R / R9-07-R**（缺身份/冲突编号仍获批） | 清单 SHA **必须 12 或 64 位十六进制**、dHash **必须 16 位**（缺失、`-`、1 位一律拒绝，不再"跳过校验"）；编号规范化后**冲突即报错**（不外 last-wins）；缺失原图/未定结论/漏给结论仍整批零写入 |
| **R9-05-R / R9-06-R**（双存储部分提交/坏状态/并发） | 快照 `corrupt` → **任何写入前阻断**；决定/拒绝快照与 DB 改为**先提交 DB + 失败补偿回滚**（`COMPENSATED_APPROVAL_ROLLBACK`：按变更前状态复位 `enabled` 与 note），避免把 DB 事务悬在写盘期间引发并发写锁争用；`load_rejections` 以 **DB 为权威**（`enabled=1` 的行不因 JSON 残留"撤回"而被计为当前拒绝） |
| **R9-08-R**（未知政策被当默认真值） | `_service_policy` 读不到服务真实字段 → **返回空 + `review_policy_source="unknown"`**（不再回填 0.90/0.60/0.90）；在线 shadow 增加 `review_policy_unknown` 例外面、离线 `detail_blockers` 要求来源必须是 `service`，两边一致"不声称会改变判定"；按您建议给旧 `SyntheticModels` **显式补 `policy_snapshot()`**（业务断言未改） |
| **R9-10-R**（窗口 CLI 三用法 SQL 失败） | `WHERE` 拼在 `ORDER BY` **之前**且参数化；观测、`total`、`image_total` **共用同一 UTC 半开范围**（起点含、终点不含，窗外不计入）；默认口径仍如实标"全库留存累计" |
| **N-IDENTITY**（身份核验假阳性） | 工具重写：强制 SHA/dHash 格式与长度、**核清单 dHash**、核 `DECISIONS.json` 的 `batch` 与目录名一致、按**当前决定的 `source`** 追溯（不再扫任意旧批次）、`--batch` 只作**范围断言**（不覆盖 provenance）、拒绝快照 `corrupt` → **非零退出**、JSON 报告写为纯 ASCII（跨默认编码可读） |

### 我引入并已修掉的一处回归（如实登记）

为满足 R9-03-R，我最初把"必须有结构化快照"做成硬要求，**打掉了您第八轮的 8 项 group 探针**（它们用注入 loader、无快照）。已补**显式兼容路径**（见 R9-03-R 第 ④ 点）并恢复通过：196 项入库探针全过。

---

## 三、探针平台适配与 AST 口径更正

- 新增第 **4** 处文档化适配：窗口探针用 `read_text()`（无 encoding）读取**本仓库 UTF-8 报告**，Windows 默认 GBK 抛 `UnicodeDecodeError`（macOS 通过）→ 只补 `encoding="utf-8"`，**断言与意图逐字未改**。
- 因此 AST 口径应为：**18 个文件 / 14 个一致 / 4 处已解释差异**（mode 输入隔离、scope 契约调整、群探针 Windows 路径注入、窗口探针读取编码）。我上一稿写的"11/14"与您更正的"15/18"都请以此为准。

---

## 四、B 方案（54 群）与批准集合的**只读对账**（您第 3 步要求）

| 项 | 结果 |
| --- | --- |
| 本次新增 | op_id `658e9770b7f7`：归属 **54** 行 + 路由 **54** 行 |
| 人数原件 | `docs/evidence/stats/groups-20260919T135629Z.md`（140 群，**>200 人 = 67 群**） |
| 对账 | **54 ⊆ 67**（`subset=True`，无例外）；未新增的恰好是**既有已可路由的 13 群** → **54 = 67 − 13** |
| 如实登记的缺口 | 执行当时**没有** `groups-*.json` 结构化快照，工具按 `action_enabled=1` 选择（人数依据来自上述 md 普查）——您指出的"授权范围交接"问题；**新工具已改为必须绑定结构化快照 + 阈值参与选择** |
| 未改事项 | 未重跑生产函数、未撤销任何路由、未扩大群范围、未改开关 |

## 五、仍未证明（不变）

A09 前 6 秒归属、历史授权快照（`NOT_PROVEN`）、导出多查询一致性读事务、**Windows 回滚全链实机演练**、一次未复现失败、enforce 最终批准与阈值校准、B 的实际动作效果（可路由 ≠ 已产生真实动作）。审计里的 `stage_runtime_proven=false` 只是如实标注，**不构成"服务已加载 recall_only"的证据**。

## 六、请主审重点复核

1. **R9-03-R 的兼容路径**（`injected_loader`）：为了让您第八轮 8 项探针继续有意义，我保留"注入名单执行"通道并如实标 NOT_PROVEN——是否可接受，或应改为**一律拒绝**（那会需要修改旧探针的 fixture）；
2. **R9-05-R/R9-06-R 的双存储方案**：我选择"**先提交 DB + 失败补偿回滚**"（而非 DB 事务内写文件），以保住并发语义；是否需要进一步收敛为"同库事务保存权威决定、JSON 仅导出"（那会改动 `image_allowlist` 结构或新增表）；
3. **B01 的判定**：我把"该群存在其它 provider 已启用 settings 行"视为冲突（含官方隐式默认）——生产上这 54 群只有 `onebot` 行，故本次操作不受影响；
4. **第 4 处适配**（窗口探针读取编码）是否可接受；
5. **`194eb0b` Windows job 的 30s alembic 子进程超时**：是否认可按"与改动无关的 CI 抖动"处理（我已触发重跑，结论以重跑为准），还是要求我把该测试的超时阈值一并调整。

---

## 七、可直接转发的回评

见同目录 `docs/2026-09-20-r132-round11-review-reply.txt`（纯文本，可整段复制）。
