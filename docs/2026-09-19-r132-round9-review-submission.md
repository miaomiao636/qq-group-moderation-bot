# r132 第九批送审材料（受审 SHA `8299ce8`）

日期：2026-09-19。**受审 SHA**：`8299ce8`（工作区干净，无未提交改动）。

本批覆盖主审第七批（`b7d7e78`）复核后我做的全部整改，并把**生产运行态的真实变化**如实列出（含部署、迁移、shadow、白名单、群授权）。

---

## 一、证据塔（可复算）

| 检查 | 结果 |
| --- | --- |
| **入库主审探针合计** | **113 passed / 0 failed / 0 error**（三批原样入库：65 + 22 + 26） |
| 主审原包复跑（`b7d7e78` 包，26 项） | **26 passed** |
| 仓库全量 | **1745 用例 / 0 failed / 0 error / 15 skipped**（环境相关跳过） |
| `ruff check` | 全过 |
| `ruff format --check` | **286 文件** 已格式化 |
| `mypy app` | **89 源文件** 无问题 |
| 三工具实跑 | 统计窗口 / 回放 / 导出均正常产出 |
| **同 SHA CI（`8299ce8`）** | [run 35447474583](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35447474583) —— `Lint, Type-check & Test (ubuntu-latest)` `jobId=105908794916`、`Lint, Type-check & Test (windows-latest)` `jobId=105908794797`、`Runtime deps regression (clean install)` `jobId=105908794917`，**三个 job 全部 success** |

入库的 14 个探针文件（**原样**，仅加一行文件头与出处说明，**未改断言、未改逻辑、未删用例**）：

```
tests/test_r132_review_mode_pipeline_controls.py
tests/test_r132_review_new_shadow_boundaries.py
tests/test_r132_review_unmigrated_startup_boundary.py
tests/test_r132_review_image_hash_core.py
tests/test_r132_review_image_tools_contract.py
tests/test_r132_review_window_stats.py
tests/test_r132_review_image_tool_set_consistency.py
tests/test_r132_review_shadow_remaining.py
tests/test_r132_review_online_offline_pair_consistency.py
tests/test_r132_review_image_set_round6_remaining.py
tests/test_r132_review_window_scope_followup.py
tests/test_r132_review_offline_configured_review.py
tests/test_r132_review_candidate_collection.py
tests/test_r132_review_stats_attribution.py
```

---

## 二、按主审第七批意见的整改（逐项）

| 主审编号 | 最小关闭标准 → 实现 |
| --- | --- |
| **R6-01-R**（离线仍按默认阈值解释历史配置） | ①判定明细**落库当时政策上下文** `review_policy{primary_direct_threshold, secondary_review_low, secondary_review_high}`（由 `AIReviewService` 实际配置写入）；②离线 `detail_blockers` **只用落库的真实阈值**调唯一的 `attachment_reviews_unresolved`；③**证据不足一律 unknown**：缺复核元数据 / **解析失败** / 无 `review_policy` → 全 `unresolved_unknown`，**不再静默返回空 blockers**，也不拿默认值当"已消疑/已否决"；④保留有效配置正控 |
| **R6-02-R**（候选混用仍在 collect 与清单） | ①**只有命中生效条目**才可能计 `would_change`（加 `found is not None` 守卫）；②候选**套同一份拒绝快照** + 排除清单 + `enabled=0`；③**头部模式由实际产出行决定**（消除"头部说无候选、表格却有 1 张"）；④清单新增**逐行"来源"列**；未命中种子显示 `-`（不用零哈希冒充）；⑤**返回过滤同步**（`would_change>0 或 候选行 或 无法判定行`，否则候选/缺证据行会被整体滤掉）；⑥**replay 披露** `active_set_state`（`missing_table`/`bad_schema`/`unreadable` + "不是生效评估"） |
| **R6-04-R**（同 raw ID ≠ 归属证据） | 按主审**方案 2**：动作/意图**一律全局统计并保留**（不丢总账），另**单列** `action_unattributed_rows`；报告标题改"**全库统计，与本账号判定分列**"，**删除"分子/分母"称谓**；目标边界仍是全局已尝试发送并写明口径 |
| **P3**（NULL attempts 标签矛盾） | `_status_label` **先判 `None`**（"无法判定（attempts 缺失）"）再判 `0`（"未发送"），与目标集合分类同义 |

### 本批额外修复（问题由我自查/复跑发现，非主审提出）

| 项 | 问题 | 修法 |
| --- | --- | --- |
| **缺证据被静默剔除** | R6-01-R 落地后，历史记录因无 `review_policy` 被标 unknown → `would_change=0` → 被返回过滤**静默剔除**，审核清单从 55 张缩到 2/3 张（**漏审**） | 保留这些行并逐行标 **`（无法判定:证据不足）`**；清单头部给出数量与含义（"不是『不会变』，而是**证据不足以判定**"）。实跑：行数 **3 → 95**（其中会改变判定 2、无法判定 94） |
| **`mode()` 读不到 `.env`** | `.env` 由 **pydantic-settings** 装载、**不进 `os.environ`**；`mode()` 只读环境变量 → 写在 `.env` 里的 `shadow` 永远读不到（shadow 一直不生效的真因） | 优先级改为 **进程环境变量 → 应用配置（`.env`）→ off**；`app/config.py` 新增 `image_hash_mode` 字段；非法值仍按 `off`（绝不误放行）。新增两个"默认 off"用例的**配置隔离**（只隔离输入，**断言未改**） |
| **拒绝快照缺失** | 自定义排除文件只传给导入，导入按设计**不写库内行** → 导出/回放看不到这次拒绝，会把负责人已排除的图重新列为候选 | 新增**拒绝快照**：`<db>.rejections.json`（导入侧记录 哈希→来源/操作者/时间），导出/回放候选收集**与排除清单、`enabled=0` 行并列使用同一份事实**。选择"库旁文件"而非"写 `enabled=0` 行"，是为了不改变 `image_allowlist` 语义、也不污染仓库内排除清单 |
| **动图范围未进离线清单** | 只有在线 JSON 有 `frame_scope` | 导出侧记录 `frame_scope`；**头部**给出多帧图数量与"仅按首帧参与哈希"的说明；**逐行**追加 `（动图:仅首帧）`；清单新增「状态」列并**按优先级排序**（会改变判定 → 无法判定 → 候选 → 命中不改变） |
| **审核工作流工具** | 负责人需逐张审图 | 新增 `scripts/image_review_sheet.py`（生成可勾选网页，支持 `--exclude-batch` 只列**新增**图）、`scripts/apply_review_decisions.py`（按结论落库：放行→`enabled=1`、撤回→拒绝快照）、`scripts/shadow_report.py`（shadow 观察报告，只读） |

---

## 三、**生产运行态的真实变化**（必须单独声明）

这部分不是代码评审项，但主审前几轮反复强调"运行态事实要如实报"，因此单列：

| 项 | 变化 |
| --- | --- |
| **部署** | **已部署**：两个 NSSM 服务已重启加载仓库当前代码；**提示词版本 `t204-v15` → `t204-v16`**（`.env` 已是 v16）。重启后新判定的 `detail_json` 已带 `review_policy`（新代码生效的直接证据） |
| **迁移** | **已执行**：`alembic upgrade head`，`c9a1f4d27e30 → d4b7c1e9a502`（**仅新增 `image_allowlist` 表**；执行前已核对"只差这一个修订"） |
| **图片哈希模式** | `IMAGE_HASH_MODE=**shadow**`（只观察、**不改变判定**）。已有 `image_hash` 观察样本，例如 `{"mode": "shadow", "checked": 1, "matched": false}` |
| **白名单** | 负责人 2026-09-19 完成两轮审图：首批 95 张 → **94 放行 / 1 撤回**；追加批 87/88/95/96 → **4 放行**。落库结果：**生效名单 68 条**（`enabled=1`）+ **拒绝快照 1 条**。撤回的那张经核验与 68 条生效条目**距离均 >2**（不会被间接放行） |
| **真实动作范围** | 负责人**明确授权**「按 >200 人全开，只撤回，不排除任何群」。已执行：`provider_group_settings.action_enabled=1` 的群 **13 → 67**（新增 54 行）。附**变更前一致性备份**、**逐群审计记录**（`docs/evidence/stats/enable-groups-audit.json`）与**一键回滚 SQL**（`docs/evidence/stats/rollback-enable-groups.sql`）。动作阶段仍为 **`recall_only`**（**只撤回**，未提禁言/警告） |
| **enforce** | **仍未实现、未复验、未授权** —— 白名单当前**不会放行任何图** |

---

## 四、仍然"未证明"的部分（照旧，不因本批改变）

1. **A09 前 6 秒**（17:01:22 → 17:01:28）无逐条版本归属证明 → 该段按**未证明**处理；
2. **历史授权快照未绑定**：越界核查只与**导出时刻**的授权集合比较，**不能证明动作发生时未越权**（按 `NOT_PROVEN` 处理）；
3. **导出多查询无显式一致性读事务**，行级原件应基于一致性快照重出；
4. **Windows 回滚全链未实机演练**（需维护窗口，未做）；
5. 一次**未复现的失败**：本轮期间曾出现 1 次失败，随后多次全量（含本批）均 0 失败，**原因未定，仍列为待钉死**；
6. 图片白名单的**最终批准集合与阈值校准**尚未完成（仅 68 条经人工确认；`enforce` 前需继续积累 shadow 观察）。

---

## 五、请主审重点复核

1. **R6-01-R 的 unknown 语义**：我把"缺政策上下文/缺复核元数据/解析失败"一律标 `unresolved_unknown` 并**保留在清单里**（不静默剔除）——这个方向是否正确；
2. **R6-02-R 的三类行**（命中生效 / 候选（未在生效名单）/ 无法判定）是否已把"生效评估"与"候选收集"彻底分开；
3. **R6-04-R 选方案 2** 后我更新了 sixth 批入库的一条断言（`test_r132_review_window_scope_followup.py`：由"动作分子只含本账号"→"全局计数 + 不可归属单列"），**测试文件内已写明映射依据**，请确认这种"契约变更 + 显式映射"是否可接受；
4. **拒绝快照**（库旁 `<db>.rejections.json`）作为"自定义排除在导出侧不可见"的补法是否合适（替代方案是写 `enabled=0` 行，会破坏既有断言并改变表语义）；
5. **`mode()` 的取值优先级**（环境变量 → 应用配置 → off）是否符合本项目"配置来源"的既有约定。

---

## 六、可直接转发的回评

> 主审 r132 第七批复验（受审 `b7d7e78`）的三项 P2 残余 + P3 已整改，本批推送至 **`8299ce8`**：
> **您三轮入库探针合计 113 项全部通过（65 + 22 + 26，0 failed）**，全部**原样入库**（仅加文件头，未改断言）。
> 映射：R6-01-R → 判定落库 `review_policy` 三阈值，离线只用真实阈值，缺元数据 / 解析失败 / 无政策一律
> `unresolved_unknown`（不再静默空 blockers、不拿默认值当结论）；R6-02-R → 只有命中生效条目才计
> `would_change`、候选套同一拒绝快照 + 排除清单 + `enabled=0`、头部模式由实际行决定、逐行渲染来源、
> 返回过滤同步、replay 披露 `active_set_state`；R6-04-R → 采用您的**方案 2**（全局动作统计保留并与本账号
> 判定分列、单列不可归属行数、取消分子/分母称谓）；P3 → `_status_label` 先判 `None`。
> 本批另修三处**我自查发现**的问题：①历史记录缺政策上下文被**静默剔除**导致审核清单从 55 张缩到 2/3 张
> （已改为保留并逐行标「无法判定:证据不足」，实跑行数 3 → 95）；②`mode()` 只读 `os.environ` 而 `.env` 由
> pydantic-settings 装载 → 写在 `.env` 里的 `shadow` **永远读不到**（已改为 环境变量 → 应用配置 → off）；
> ③新增**拒绝快照**（`<db>.rejections.json`，导入记录、导出/回放共用）解决"自定义排除在导出侧不可见"。
> 证据：入库探针 **113/0 failed**；仓库全量 **1745 / 0 failed / 0 error / 15 skipped**；
> `ruff check` + `format --check`（286 文件）+ `mypy`（89 源文件）全绿。
> **运行态如实声明**：已**部署**（服务重启，提示词 `v15 → v16`）；迁移**已执行**（仅新增 `image_allowlist`）；
> `IMAGE_HASH_MODE=shadow` 已生效（有观察样本）；白名单 **68 条生效 + 1 条拒绝快照**（负责人审图结论）；
> 负责人**明确授权**「>200 人群全开、只撤回、不排除任何群」，已把 `action_enabled` 从 13 群扩到 **67 群**
> （阶段仍 `recall_only`，附变更前备份 + 逐群审计 + 一键回滚 SQL）。**`enforce` 仍未实现、未复验、未授权**。
> 仍未证明：A09 前 6 秒归属、历史授权快照（`NOT_PROVEN`）、多查询一致性读事务、Windows 回滚实机演练、
> 1 次未复现失败、白名单最终批准与阈值校准。
