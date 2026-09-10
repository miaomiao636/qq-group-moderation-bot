# PR #5 R-105主审整改与代码验收

日期：2026-09-10。对照原整改评论 `issuecomment-5613457246`，审查起点为PR首轮head `6304d35`；PR目标主分支基点 `db3edb4`。完整PR包含T-303实施记录、T-307、管理面板/API、AI/反馈增强及安全整改，不仅是一个动作Adapter。

## 当前结论

本轮代码与文档覆盖原17项中的15项；P1-13与P1-14保留现场验收，不能靠代码或报告制作关闭。最终本地门禁已通过，两轴复核在本次审查范围内均无剩余代码阻断。合并还要求当前提交的远程CI通过；最终CI链接与合并SHA记录在PR新评论，避免自引用提交。默认SHADOW、OneBot动作关闭、阶段recall_only；本次未操作真实QQ或Windows。

## 原17项逐项核对

| 原编号 | 实际整改与核验入口 | 结论边界 |
|---|---|---|
| P0-1 鉴权 | 独立读/写令牌与scope、会话TTL、真人CSRF；`test_api_auth_security.py`、`test_admin_hardening.py` | 本地验收通过 |
| P0-2 Agent确认 | DB完整参数/身份/版本计划、真人预览批准、原子单次执行；重新启用审核也不能复活动作绕审批；`test_agent_confirmation.py`、`test_admin_hardening.py` | 同上 |
| P0-3 条件双模型 | 疑难第二模型、完整主次配对veto、失败不回退单模型、来源/缓存/策略隔离、严格数值置信度；`test_ai_conditional_review.py`、`test_ai_rule_conflict_veto.py`、`test_pipeline_evidence_veto.py` | 代码与假响应验证；真实准确率/耗时/费用仍需W2 |
| P0-4 反馈学习 | 停止即时通用prompt反馈；最新人工标签、3消息/2成员候选、失效重算与复制前核验；provider隔离，显式误判纠错；`test_feedback_learning.py`、`test_feedback_vision.py` | 不代表自动训练或自动发布 |
| P0-5 校园墙prompt | 删除全局无条件放行；prompt内置v4与缓存隔离；负责人群规的本地来源规则保留为可审计规则 | 模型不能据此泛化全局白名单 |
| P1-6 SSRF | 逐跳全DNS检验+已验IP绑定、原Host/TLS SNI、不走环境代理、总时限、临时文件清理；`test_ssrf_media.py`、`test_media_pinning.py` | 本地验收通过 |
| P0-7 OneBot语义 | 仅ok+严格整数0成功；模糊/async/矛盾/发送后超时UNKNOWN；等锁/发送/echo总时限；`test_onebot_actions.py` | 未调用真实OneBot |
| P1-8 唯一出口 | 明确provider与唯一owner；动作链每次重验群开关/路由；`test_provider_routing.py`、`test_group_admin_routes.py` | 不做未验证的跨通道ID转换 |
| P1-9 provider复合键 | `b1d3f5a7c909`新增ProviderGroupSettings/审批计划/owner，保留旧表，升级与降级均关闭旧授权；`test_migration_r105_admin.py` | Windows升级前备份，逐群真人重新批准 |
| P1-10 self_id | 固定专用账号，拒绝第二连接/不同账号接管，旧socket不能完成新请求；`test_onebot_ws.py`、`test_onebot_actions.py` | 账号绑定真实值由Windows负责人配置 |
| P1-11 急停 | DB共享、逐外呼重读；立即激活，解除须真人批准；原子意图领取避免并发双发；`test_admin_hardening.py` | 在途已发送请求不能被撤回，仍需核对UNKNOWN |
| P1-12 先持久化 | `c2e4f6a8b010` durable inbox、单实例OS锁、有限重试与租约、重启恢复、原始payload清理；`test_onebot_inbox.py`、`test_onebot_ws.py` | 不保证上游没送达的事件能补回；W1量化缺口 |
| P1-13 T-303证据 | 新清单提供匿名化/摘要/版本/计数/原件索引规范 | **未关闭**：所指文件在Windows，当前PR没有可独立核验的脱敏原件；现场核验或重跑W1 |
| P1-14 W2独立验收 | 新 `app.reports.evaluation` CLI严格元数据、混淆矩阵/分类型P95、拒混版本、非验收一致率提示 | **未关闭**：工具不是评测结果；Windows真实供应商与独立样本待测 |
| P2-15 CRLF | 保留原 `.gitattributes` 行尾约束，最终diff/lint/format核验 | 不再夹带全仓格式化 |
| P2-16 PR边界 | PR标题/正文应列实际包含的T-307、管理控制、AI反馈、持久接收、交付准备，保留历史提交不重写 | 不用“T-303已验收”掩盖证据缺口 |
| P2-17 文档 | AGENTS/Context/Decisions/README/任务/进度/交接/spec/切换手册/Windows清单一致更新 | 以D-022现行策略为准，历史报告保留来源标注 |

## 交叉复核额外修复

- Standards轴：审批有效动作复活绕过、动作同意图双发、发送阶段无超时、SQLite WAL备份漏数、升级旧授权未撤销；修复后按独立回归复验。`routes.py`职责较多仅是后续渐进拆分建议，不以此批量重构。
- Spec轴：未知/缺失媒体降级被AI覆盖、模型布尔置信度、旧标签仍参与候选、不同版本报告混算、文档只撤回阶段缺少实际门禁；增加最终veto、严格契约、最新真值、分版本报告和`ONEBOT_ACTION_STAGE`。
- 一致性备份按DATABASE_URL使用SQLite online backup，quick_check成功才报成功；备份只含数据库，不含媒体/凭据，不等于异机灾备。
- 原“自动清理”开关没有实际调度入口，补 `python -m app.reports.maintenance cleanup` 与最近执行状态；Windows任务仍需现场注册与验证，界面不再把开关开启当作计划已运行。
- 三worker共享刷屏计数但保留各自规则快照，避免连续三条被拆成三套独立计数。
- 人工确认正常/误判经精确Shadow映射撤销有效违规并同事务审计；真值按新增记录ID而不是易受Windows时钟回拨影响的时间戳。不能自动恢复旧处罚、关闭案件或替代人工解禁。
- 对既有迁移与ORM做一致性校验：补登记保留的member_aliases、既有中立列及索引，不执行删除迁移；新增 `test_schema_metadata.py` 先失败再通过，防止未来自动迁移误删历史数据。
- 双会话违规计数原可出现[1,1]，现事务串行得到[1,2]；同成员完整动作链锁保证禁言顺序[3600,86400]，不会被迟到1h覆盖24h。等待超时30秒安全SKIPPED并审计转人工，取消/异常均释放资源；不同成员仍并行。独立Spec probe及96项相关测试通过，完整门禁另见下节。

## 最终验证记录

- 完整pytest：**556 passed / 1 skipped / 1 warning，103.60s**。跳过项为测试机本地真实媒体样本；旧迁移测试的Python 3.12 datetime adapter弃用提示为非阻断已知警告，未隐藏。单测时长不是W2业务延迟。
- mypy：71源文件通过；ruff check通过；ruff format --check：144文件已符合格式；git diff --check通过。
- 独立临时数据库：upgrade head→downgrade base→upgrade head成功；旧授权升级/安全降级与历史数据保留测试通过；最终alembic check无新增差异。生产数据库未操作，不建议生产降级到base。
- 独立仅运行时环境：按锁文件 `uv sync --locked --no-dev` 安装，应用可导入且pytest不可导入；未修改锁文件。
- Standards轴：有效动作审批绕过、幂等、发送总时限、WAL备份、旧授权、metadata兼容与可执行交付文档均已复核；本范围无剩余代码阻断。
- Spec轴：不可审内容最终veto、严格置信度、最新反馈真值与纠错、版本隔离、阶段门禁、同人动作顺序已独立复测；本范围无剩余代码阻断。
- 远程Ubuntu、Windows、干净运行时CI必须匹配本轮PR最终head；链接与结论发布到PR新评论，不能引用首轮 `34434789028` 冒充本次结果。

每次高风险回归先复现失败，再做最小修复；不将修复前基线370项或旧CI作为当前证据。测试与独立复核不是“所有场景永无问题”的保证，Windows能力/样本外效果仍由下述现场门禁确认。

## 排期与交付

可在默认关闭下合并的是代码，不是Windows整体验收。P1-13排Windows更新/备份后首先执行，P1-14准备独立样本并在W1通过后执行；之后才是隔离W3、恢复W4和目标群W5，详见[实测清单](windows-delivery-checklist.md)。客户主动告警渠道、接管人、响应时限、隐私告知与备份责任尚需明确；未完成时不能按无人值守服务交付。首批不要自动踢人。
