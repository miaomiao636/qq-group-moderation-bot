# QQ 手机辅助巡检（PHONE-INSPECT-20260921）

负责人已授权制作。代码在 `app/phone_inspector/`，由独立桌面进程运行；不读取主服务配置、不连接生产数据库、不调用审核或处罚出口。负责人已拒绝名单匹配，本工具不使用样本名单作为判据。

## 使用

启动 `uv run python -m app.phone_inspector`，或使用桌面 `QQ 手机辅助巡检.lnk`。快捷方式指向本仓库 `.venv/Scripts/pythonw.exe -m app.phone_inspector`，依赖本仓库和虚拟环境保留原位置。需要带 Tk 的 Python、Android Platform-Tools，以及已通过 USB 调试授权的安卓手机。ADB 可在界面选择；本机工具放在 `%LOCALAPPDATA%/QQPhoneInspector/platform-tools/`。

1. 手机打开目标群的成员列表，选择默认排序，保持解锁、竖屏、QQ 前台。
2. 先选“试扫 10 次资料卡”并新建任务。确认连接与结果后，可选择更大的范围。
3. 扫描期间不要操作手机。点击“暂停”会停止后续操作；已发出的读取有超时上限，暂停并非瞬间结束当前命令。
4. “继续当前任务”会从顶部重新核对并合并去重，**不是从断点直接跳过已看成员**。切换群时新建任务。
5. 暂停后导出 TXT、CSV、JSON，异常提示记录包含群名称、群号、QQ 号、观察时间及本地证据目录。

任务默认在 `%LOCALAPPDATA%/QQPhoneInspector/tasks/`。每个任务有独立数据库及追加保存的证据，导出不会覆盖先前导出。保留整个任务目录才能同时保留截图、原始异常页面和执行记录；仅复制 CSV 不包含这些证据。界面仅显示最近观察的有限行，完整异常记录在导出中。

## 判断与边界

- 只确认 QQ 原生资料卡明确显示的“该账号状态异常，涉嫌被多人举报或存在违规行为，暂不支持查看资料卡。”，并读取资料页的明确 QQ 号。昵称、默认头像、空资料不是异常判据。
- “本次未观察到提示”不等于账号正常；弹窗的出现时间、客户端缓存、网络和平台状态均可能影响观察。本工具不认定永久封禁、注销或已证实违规。
- 先读取群身份，再沿同一设备、安卓用户/QQ 分身、已知页面连续导航。普通资料卡和个性封面资料卡采用不同原生身份字段；无法确认时暂停，不猜测账号。
- 到达列表末尾必须同时满足滚动后布局及实际 QQ 序列重复，再核对前后群人数。群成员实时变化可能使人数无法对齐；到达末尾不是某一时刻完整快照或全群无遗漏的证明。
- 恢复合并保留历史异常观察，后续未出现提示不抹掉历史记录。未确认次数保留中断历史，不能当作独立异常账号数量。
- 首版一次巡检一个当前群；不自动切群、不发送消息、不加好友、不踢人，不提供后台无人值守承诺。原生“机器人”分组且带机器人标记的行不作为个人 QQ 巡检；同名普通成员不按昵称排除。群人数对账也不能用来掩盖此范围差异。
- 当前页面适配锁定 QQ `9.3.60`。其他版本或新资料卡布局需要先验证；锁屏、切换应用/分身、未知弹窗、页面变化或连接异常会暂停。

## 实现与验证证据

来源提交 `ac4abac6e1986d8d9acf19c57a67a2809c13deeb`；内部测试入库 `950ad424b5ab8f51aa8d0f8baab64af67bdacdd2`。恢复执行记录补丁 `d5590db28bcce622080025a0ba39ca3f3a9b141a`，回归入库 `47d11593ee070dd14aaec9047d1095f9fe1d5fa6`。个性封面资料卡补丁 `5ac5f07`，回归入库 `97b7152600be817890c272eb5146eb8f37e344f0`；全屏封面受控滑动补丁 `99389ed`，回归入库 `e7116bc282a61fc2f05a57c663c92c594978804a`。所有测试使用合成账号，不是外部主审探针；本轮未改动主审探针。

执行 SHA `47d11593ee070dd14aaec9047d1095f9fe1d5fa6`：

```powershell
uv run pytest -q --no-header --tb=short --junitxml=C:/Users/81596/AppData/Local/Temp/qqbot-phone-product-validation/full-47d1159.xml
```

实际命令将输出重定向到同目录 `full-47d1159.log`。JUnit 为 1980 项、1964 passed、16 skipped、0 failed、0 error；此结果属于个性封面补丁之前，不替代最终版本验证。

执行 SHA `e7116bc282a61fc2f05a57c663c92c594978804a` 的静态门禁：

```powershell
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
```

均通过，格式检查 327 文件、类型检查 100 文件。聚焦回归命令见下，需以最终执行结果更新状态：

```powershell
uv run pytest tests/test_phone_inspector_pages.py tests/test_phone_inspector_storage.py tests/test_phone_inspector_engine.py tests/test_phone_inspector_device.py -q --no-header --tb=short
```

后续执行 `e7116bc282a61fc2f05a57c663c92c594978804a`，全量命令同上、JUnit 与日志文件名改为 `full-e7116bc.xml` / `full-e7116bc.log`：1986 项、1970 passed、16 skipped、0 failed、0 error。同次 JUnit 中 `tests.test_r132_review_*` 子集 31 文件 / 262 项、无失败/错误/跳过。此结果属于边缘行补丁之前。边缘残行和原生机器人分组的源码修复 `599016b`，内部回归 `7990a9184dbd8de6acf2466e2ee7c2724b201283`；最终验证以下一轮实际记录为准。

## 实机验证记录

本机私有证据目录：`C:/Users/81596/AppData/Local/Temp/qqbot-phone-product-validation/`。真实成员资料、截图及群号不进入 Git。每次产品执行在任务中保存执行 SHA、命令参数、源码摘要和手机/群上下文；恢复后的执行记录在 `passes/` 中，不能借用最初任务的旧 SHA。

- 执行 `fefc0c1d00eb3e283a40ef21f417086fca7610da`，`uv run python -m app.phone_inspector --cli --adb C:/Users/81596/AppData/Local/Temp/qqbot-android-validation/platform-tools/adb.exe --output-root C:/Users/81596/AppData/Local/Temp/qqbot-phone-product-validation --limit 3 --export`：本轮查看上限 3，实际已确认 3 个不同 QQ，未观察到异常提示，按上限暂停，导出成功。这仅验证首屏普通资料卡及存储导出。
- 执行 `47d11593ee070dd14aaec9047d1095f9fe1d5fa6`，同一 CLI 改为 `--resume C:/Users/81596/AppData/Local/Temp/qqbot-phone-product-validation/tasks/20260921T105257Z-b4e7acb3 --limit 16 --export`：在个性封面资料卡暂停；累计确认 4 个 QQ、0 个异常提示、1 次未确认。没有把未知页面标成正常，随后据原始页面补适配。
- 执行 `97b7152600be817890c272eb5146eb8f37e344f0`，同一恢复命令的 ADB 路径改为 `C:/Users/81596/AppData/Local/QQPhoneInspector/platform-tools/adb.exe`：个性资料卡身份行成功读取，后续全屏封面遮挡身份字段，再次保护性暂停。累计确认 6 个 QQ、0 个异常提示、2 次未确认。原始页面 `debug-20260921T110902Z/page.xml` 与人工受控滑动后的 `debug-20260921T110942Z/page.xml` 用于确认新适配结构；这次人工滑动不属于产品自动翻页证明。
- 执行 `e7116bc282a61fc2f05a57c663c92c594978804a`，与上一条同一恢复命令：全屏封面受控滑动及身份读取通过，累计确认 10 个 QQ，滑到下一屏时因边缘残行没有可见姓名节点而暂停。该页另含原生机器人分组。依据 `qqbot-android-validation/page-20260921T111558Z-97859ee7/page.xml` 修复残行处理并明确机器人范围；没有把暂停记录改写为完成。
- 后续有界实机结果、最终全量与 CI 待实际完成后回填。尚未完成整群遍历验收，不以模拟用例或此前代理手动观察代替产品实测。

## 因生产故障切换优先级后的状态

执行 SHA `e7116bc282a61fc2f05a57c663c92c594978804a`，恢复命令同上（永久 ADB 路径、`--limit 16 --export`）：个性卡和全屏封面已连续自动读取，累计确认 10 个 QQ、0 个异常提示、2 次历史未确认。首次成员列表滑动后，底部裁剪行缺少昵称控件，触发保护性暂停。该次任务 pass 4 的 UTC 范围为 `11:11:45.211822` 至 `11:15:30.779481`，不能宣称已完成预定上限或整群遍历。

截断行和原生机器人分区适配提交 `599016b`，内部回归 `7990a9184dbd8de6acf2466e2ee7c2724b201283`。只跳过界面明确的“机器人”分区且带原生机器人标识的条目，不按昵称识别机器人。已用保存页面离线确认；随后 pass 5 因负责人反馈生产下载故障而主动停止，保留在途未确认记录。该补丁后的连续实机、自动异常提示取证、全群覆盖仍未验收，桌面快捷方式不代表这些验收已经完成。

上述产品代码和内部回归已被后续执行 SHA `d7ceaf320a22599595ff0aca0200fb7b8f2afc77` 的仓库全量覆盖，命令、最终数量及 CI 核对方式见 [媒体容量修复记录](2026-09-21-media-quota-recovery.md)。这是代码回归，不能替代手机现场验证。

主服务随后因负责人批准的媒体容量恢复而重启，分页和稳定性代码已加载，见同一记录；手机巡检仍是独立进程，不是主服务启动任务。方案 A 外部裁定及 Windows 回滚全链维护窗口仍是原有独立待办。
