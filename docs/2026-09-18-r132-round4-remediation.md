# r132 四轮复验整改与复评请求（2026-09-19）

- **受审基线**：`97d68d1957466fcbb87f259c552c59f6f52c8781`（主审四轮结论：上轮 4 项可关闭；
  本轮新提 R09-D、N-F05-2-R、F06 三处执行断点；应用探针 4 failed / 57 passed）。
- **整改后本机复现（主审探针原样入库后运行）**：`tests/test_r132_threshold_matrix.py`、
  `tests/test_r132_source_collection_contract.py`、`tests/test_r132_f05_round3_state_drift.py`
  合计 **61 passed**（整改前同一批 4 failed / 57 passed，失败项名与主审报告逐字一致）。
- **本机全量**：**1574 passed / 15 skipped / 0 failed**（junit 收集 1589）；ruff check+format、mypy 通过。
- **部署**：仍未部署（生产仍是 17:18 加载的 `t204-v15` + 当时 `wall_pair`）。

## 1. 逐项映射

| 主审编号 | 整改实现 | 回归 |
| --- | --- | --- |
| **R09-D**（P1，既有漏洞） | `app/moderation/wall_pair.py`：来源资格改为基于**完整附件证据**——`attachments = vision ∪ degraded`；其中任一 `degraded` / 需人工 / 类别非空 / 降级原因非空 → **整条消息不作来源**。正常 QR、前缀与布尔同源、在途保护边界不变 | `tests/test_r132_source_collection_contract.py`（34 项，含超时/限流/读取失败与"不处罚源消息本身"控制） |
| **N-F05-2-R**（P2） | `app/moderation/allowlist.py`：`row_versions` 从"仅 touched 行"扩展为**文件内全部成员**（并入预览指纹）；`apply_member_import` 在写锁内**逐行复核批准时的行版本**（SQL 条件比对），漂移即 `ConcurrentMemberChangeError` 整体回滚并要求重新预览；不用"重新启用"凑齐、不停用文件外成员 | `tests/test_r132_f05_round3_state_drift.py`（8 项：delete / disable / note 三变体 + 控制组） |
| **F06-A**（P1） | 默认入口 `app.core.config` → **`app.config`**（提前是 `ModuleNotFoundError`，此时服务已停、无法进入备份） | `tests/test_r132_rollback_preflight.py::test_default_entry_uses_the_existing_config_module` |
| **F06-B**（P1） | 检查程序**复制到仓库外**（`$env:TEMP`）后全程使用；`check-version` 改为**只依赖标准库**（不再导入 SQLAlchemy；所有 `app.*` 延迟导入）；`git switch` 之后由调用方在切换后的工作树内联取代码 head 并 `--code-head` 显式传入 | 同文件 `test_rollback_script_uses_out_of_repo_checker_copy`、`test_version_checker_runs_outside_the_repository`（拷到仓库外子进程实跑，含不一致必须非 0） |
| **F06-C**（P2） | 每个服务 `sc.exe start` **单独** `Assert-ExitCode`；随后轮询 `Get-Service … .Status`，两个服务都进入 `Running` 才打印 `ROLLBACK_DONE`（否则 `exit 1` 中止、不报告完成） | 同文件 `test_rollback_script_checks_each_service_start_individually`、顺序门禁 |

## 2. 请主审重点复核

1. **R09 全附件来源资格**：`vision ∪ degraded` 的覆盖面是否完整（是否还有其它"未定论"落库形态，
   例如 `cache` / 文字通道的降级）；修复后**不得**因新约束处罚源消息本身（源图仍按普通流程判定）。
2. **N-F05-2-R 完整状态复核**：`row_versions` 覆盖到"文件内全部成员"后，是否还存在**未纳入复核**的
   合法漂移路径（例如 `updated_at` 为空的旧行、跨 provider、`to_add` 目标的并发插入）；
   既有保证（组合一次 UPDATE、外部撤权/删除/ABA 拒绝、其它 provider 隔离、终态原子性）未被削弱。
3. **F06 默认入口与目标树可达性**：默认 CLI（不传 `--database-url`）路径、以及"切换后工作树"
   场景下的可执行性；启动失败必须以非 0 中止。
4. **证据引用**：`97d68d1` 对应 CI run `35344025260`（三 job success）；`35343211214` 的 head 是
   `70c4422` —— 已在三轮报告中更正。

## 3. 如实登记的缺口

- **Windows 实机回滚全链演练仍未做**（未停生产服务、未在生产库演练）；本轮只做到：脚本逻辑与
  静态门禁 + 检查程序在仓库外子进程实跑；**不足以替代实机编排验证**。
- **本次整改未部署**：生产现场不体现以上修复（尤其 R09-D 是既有漏洞，生产大概率仍在生效）。
- 固定 UTC 半开窗口的动作统计原件仍待补。
- 主审本轮 F06 的 6 项独立诊断在我本机因其路径断言与中文路径不兼容而报 ERROR，故改以独立命令
  逐条核实（`app/core/config.py` 不存在、`68a94b9` 无该脚本与依赖、启动段未查退出码），未以"跑不过"
  当作结论。

## 4. 未改动（保留）

D-037 / D-038 / D-039 政策、口径 C 窗口豁免范围、R09 前缀语义、同秒不豁免、未完成图"不处罚也不豁免"、
账号/群/成员作用域隔离、`recall_only` 阶段与授权群边界、永不产生踢人动作。
