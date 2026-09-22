# 全项目复查与修复 — PROJECT-AUDIT-20260922

本轮修复已推送，源码固定为 `1c17ba09e5bef268a6162864830396e8d36c6107`，**尚未部署**。生产仍加载 `06324fedab4baf8d0ee14e09991c5385627a01ed` / `t204-v18`，急停保持开启。独立 QQ 空间巡检不计入本轮主项目验收。

## 已复现并修复的遗漏

| 问题 | 影响与修复 | 入库回归 |
| --- | --- | --- |
| 非 ASCII 认证输入报错 | 中文/异常 Bearer、CSRF、确认码可能引发服务器错误；常量时间比较统一使用 UTF-8 bytes，错误仍拒绝、有效会话不被误消费 | `test_auth_unicode_input.py` |
| 自动群名覆盖人工备注 | 接口等待期间人工保存会被覆盖；改为原子插入，冲突时保留已有备注 | `test_runtime_resource_ownership.py` |
| 官方备用通道连接泄漏 | 每条默认动作链创建的连接未完整释放；成功、失败、取消均关闭自有资源，调用方注入资源保持原所有权 | 同上 |
| 后台报告跨查询混入并发更新 | 日/周报计数改为单 SQL；大盘使用短只读快照，不刷新/提交调用方未提交写入 | `test_report_read_consistency.py` |
| 备份保底误认说明文件 | JSON/日志或伴随文件 mtime 可挤占最后一份 DB 保底；仅以已知 DB 文件排序，未知文件保留 | `test_backup_preservation_boundaries.py` |
| Windows junction 备份越界 | 原 `is_symlink()` 漏掉 junction；复用重解析点守卫，在生成文件前拒绝 | 同上 |
| 媒体审核依据丢失 | 语音/文件的类别和规则依据在汇总中丢失；最终决策完成后追加脱敏依据，不改变既有 verdict、actions、阈值或白名单例外 | `test_media_evidence_retention.py` |

这批是内部新增回归，不是新外部主审探针包。原有主审探针未删改，无断言或锁放宽。先在基线 `3792c81594fc9a9dfa49925a68ca69538a2e4a57` 的逐步工作树复现，再修复并冻结源码；各组命令、失败与文件哈希见 [复现收据](evidence/project-audit-20260922/reproduction.json)。不能把逐步工作树写成一次干净基线全量运行。

媒体问题曾被简化夹具误判为“严重内容被校园墙放行”；真实 `AIReviewService` 高置信短路复验保留违规、没有调用模型。已撤回该绕过判断，本轮确认和修复的是审计类别/依据丢失。不会补写历史判定，不重放或补罚旧消息。

## 固定源码验证

以下全部执行于上述完整源码 SHA，使用生产 `.venv/Scripts/python.exe`，工作目录为隔离候选 worktree；没有在生产 cwd 运行测试。完整参数、环境、日志哈希见 [验证收据](evidence/project-audit-20260922/validation.json)。

| 命令（`python` 指上述解释器） | 实际结果 |
| --- | --- |
| `python -m pytest -o addopts= -q --tb=short` | 2425 passed / 4 skipped / 无 failed、error |
| `python -m pytest tests -k r132_review -o addopts= -q --tb=short` | 262 passed |
| `python -m pytest tests/test_auth_unicode_input.py tests/test_backup_preservation_boundaries.py tests/test_media_evidence_retention.py tests/test_report_read_consistency.py tests/test_runtime_resource_ownership.py -o addopts= -q --tb=short` | 51 passed，新增回归已入库 |
| `python -m ruff check app tests alembic scripts` | 通过 |
| `python -m ruff format --check app tests alembic scripts` | 367 files already formatted |
| `python -m mypy app` | 115 source files 通过 |

同 SHA 补查跳过原因：`python -m pytest tests/test_image_engine.py tests/test_longterm_data.py tests/test_managed_copies.py tests/test_space_inspector_history.py -o addopts= -q -rs --tb=short` 得到 50 passed / 4 skipped；对应本地真实样本未挂载、目录符号链接、文件符号链接与巡检历史符号链接权限，具体节点见验证收据。不把它们统称为全部 Windows 差异，也不把此前授权原图复验重复计成本轮调用。

远程源码 CI [35712920599](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35712920599) 成功。实际 PR 合并检出、父提交、各 job 命令/数量/日志哈希与干净运行依赖反向断言见 [CI 收据](evidence/project-audit-20260922/ci-source.json)，不把合并检出伪称直接检出源码 SHA。文档提交不修改源码，但交接前仍须用 `gh run list --commit <最终HEAD>`、`gh run view <run> --json headSha,status,conclusion,jobs,url` 单独检查最终 HEAD，不借用旧 run。

## 生产只读核验与仍未完成的工作

观测执行 SHA `06324fedab4baf8d0ee14e09991c5385627a01ed`，UTC `2026-09-22T09:46:04.750789+00:00`。实际命令：`git rev-parse HEAD`；私有 `ops_snapshot.py` 的 PowerShell here-string 经生产 `.venv/Scripts/python.exe -B -` 执行；`Get-CimInstance Win32_Service`、`Get-ScheduledTask`、NSSM 注册表轮转项只读查询。SQLite 使用 `mode=ro` 与 `PRAGMA query_only=ON`，仅聚合，不导出成员或消息。命令/脚本与原件哈希、后续 E 盘核验见 [运维收据](evidence/project-audit-20260922/ops-readonly.json)。

- Web/Runtime 运行并设为自动启动，OneBot ready/online、观察时无队列积压；急停 true，不能表述为真实撤回已恢复。
- 数据目录约 3.59 GiB；媒体约 2.44 GiB，配额 20 GiB；数据库约 112 MiB。日志已配置 10 MiB 在线轮转，没有观察到近期容量告急。大小是运行中元数据快照，文件 mtime 不代表保留期源时间。
- 按任务名称、路径和执行参数检索，只发现自动清理任务；未发现匹配的定时备份或独立健康探针。自动清理最近成功是既有任务结果，本轮未执行清理。
- E 盘属于不同物理磁盘且有历史备份，但其 revision 为旧 `b8d4f2a05e31`；只读 quick_check=ok 不等于当前库 `e1c7d4b8a902` 可恢复。未据本次范围断言其他位置不存在备份，也未验证异机副本或加密。
- 待人工案件 892，最早来自 2026-09-14。分页解决页面长度，不代替人工审核；本轮不批量结案或处置成员。
- 通知按负责人既有选择继续关闭。整机故障/恢复窗口、N03 可核验清单和最终原件处置、历史缺失实机证据仍未关闭。
- 服务仍使用 LocalSystem；切换专用低权限账户涉及目录 ACL、凭据与监督器，需独立维护验证，不能顺手改权限。

## 下一步选项

| 事项 | A（推荐） | B | 代价与边界 |
| --- | --- | --- | --- |
| 本轮修复加载 | 经负责人确认后备份、正常重启 Web/Runtime，急停保持 | 暂保留源码，后续窗口加载 | A 有短暂收消息中断，不保证补齐；不迁移、不改配置、不恢复处罚 |
| 日常备份 | 安排每日一致性备份、更新 E 盘副本，并确定异机目标及恢复验收 | 保持维护前手工备份 | A 占存储并需维护；B 恢复点不稳定，不视为无人值守交付 |
| 人工积压 | 指定负责人和日常处置量，保留逐案依据 | 继续积累 | 需要人力；不以自动关闭代替复核 |

整机恢复暂待排期、通知暂不开启、N03 原件先审核清单的决定继续有效，不重复要求负责人重新选择。备份命名保底不等于完整性验收；junction 的静态边界修复不声称跨平台抵御任意目录替换竞态。

部署准备已在私有 `QQBotDeploy/project-audit-deploy-20260922-01/` 完成语法核验，计划固定生产 base `06324fe`、target `1c17ba0`，`approval_granted=false`；没有执行生产阶段。原校园墙部署授权不自动扩展到本轮全面审查修复。依据交接 §10.2，实际重启仍需负责人明确确认。
