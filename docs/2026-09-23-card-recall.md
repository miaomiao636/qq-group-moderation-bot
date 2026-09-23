# 群名片补正、微信小程序分享卡撤回与含 200 人群授权

负责人 2026-09-23 明确要求「微信小程序分享卡以后也撤回，包含 200 人的也开」。本次只修卡片结构识别并扩充明确核对的缺失群；成员白名单、群主/管理员豁免、校园墙二维码**图片**政策和两分钟图片窗口不变。微信小程序**分享卡**无来源豁免，仍经过现有未知内容/缺失媒体等安全门禁。动作阶段保持 `recall_only`，不重放历史消息。

## 事实与证据边界

执行基线 `2391bcaec843a39ef34446db51aae87665723c82`，命令 `.venv/Scripts/python.exe -X utf8 -B D:/QQBotAudits/cards-groups-20260923/audit.py`，只读收据 `audit.json`，观察时间 `2026-09-23T09:00:05Z`：

- 141 个目录群中，人数 >=200 的 72 个，完整授权 67 个；缺失下表五群的 settings/owner/route。
- 当日已处理的合并转发 18 条：17 条撤回接口返回成功；1 条因大学生创业交流群动作未启用而跳过。接口成功不等于独立手机端观察。
- 用户截图群的群名片、微信小程序卡均被旧代码识别为普通 share_card，只记录；不是所有合并转发都失效。截图内小程序卡也不是合并转发。
- 该观察时急停为 false，动作阶段声明 recall_only；生产原进程加载历史源码 `1c17ba0`，不能把当前工作树 HEAD 当作已上线。

| 群号 | 群名 | 当时人数 |
| --- | --- | --- |
| 822284640 | 大学生创业交流群 | 210 |
| 784172346 | 徐州免费家教群 | 200 |
| 594188592 | 无锡免费家教群 | 200 |
| 609631435 | 合肥免费家教群 | 200 |
| 245106525 | 济南免费家教群 | 200 |

同一基线加工作树补正，以下只读命令均使用上述 Python 前缀：

- `D:/QQBotAudits/cards-groups-20260923/read_ark_schema.py`：本机 QQ 只生成 Ark，不发送群消息；生成的群名片为 `com.tencent.contact.lua / contact / meta.contact.jumpUrl=mqqapi://card/show_pslcard?...&card_type=group`。这是真实 QQ 生成格式，不冒称取回了截图历史原始载荷。
- `D:/QQBotAudits/cards-groups-20260923/read_recent_schema.py`：身份校验前后包围的保留消息查询，取得 `com.tencent.miniapp.lua / miniapp / meta.miniapp`，`tag=微信小程序` 且 `tagIcon=https://miniapp.gtimg.cn/public/miniwx.png`。三条消息为同一模板，不当作三个格式变体。仅保留结构摘要，不发布用户卡片载荷或签名参数。
- 旧截图消息查询返回空 message，无法据此断言旧原始结构；自然 inbox 观察也未捕获 JSON。`schema-live/local-napcat-source.json` 摘录与其说明不符，**未采纳**，原件保留；采用根目录的实际 API 收据，不引用错误行号。

## 实现与验证

`tests/test_wechat_card_recall.py` 保存合成脱敏结构及行为回归，不改动主审探针。覆盖普通新闻/音乐提及小程序、QQ 小程序、个人卡、重复参数、多卡顺序、保护身份、窗口隔离、管线去重、SHADOW 零动作及 recall_only 单次撤回。`R_WECHAT_MINIPROGRAM_CARD` 使用独立规则号，复核门、AI、配对窗口、图片白名单 shadow 与离线工具共享结构硬证据集合。

`scripts/initialize_group_actions.py` 默认只读。必须逐个指定群且执行时显式填写负责人授权；实时核对账号与含 200 人人数，D/E 盘备份、写事务内复核所有前态，再同时新增 settings/owner/route。任何通道已有行、账号/阶段漂移、审计写失败均阻止整批。准备审计不等于提交证据；实际提交以数据库审计和独立提交收据为准。不是以后自动按人数扩群的定时规则。

冻结源码全量、CI、实际群授权及部署结果待完成后补录；本节不是已上线声明。
