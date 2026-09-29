"""成员白名单文件的解析与格式化（负责人 2026-09-18）。

设计约束（本模块**纯函数、零 I/O、零数据库**，便于直接单测）：

- 文件格式：UTF-8（容忍 BOM），CRLF/LF 均可，每行一个 QQ 号；
  - 空行忽略；``#`` 开头为整行注释；
  - 支持行尾备注：``123456789  # 合作方`` 或 ``123456789,合作方``；
  - ``#`` 之后的同内容一律视为备注（QQ 号是纯数字，``#`` 不可能出现在号内）。
- 校验：纯 ASCII 数字、长度 5–12；重复号只保留第一条并单独报错行。
- **不做任何归一化**：QQ 号是精确身份，绝不复用关键词白名单的变体归一化
  （那会把数字当成文本做谐音/大小写处理，语义完全错误）。

导出格式与导入格式完全一致，形成「下载 → 记事本修改 → 上传」闭环。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

MIN_MEMBER_ID_LENGTH = 5
MAX_MEMBER_ID_LENGTH = 12
MAX_MEMBER_NOTE_LENGTH = 64
# 单文件最大行数（防误传超大文件；正常白名单只需几十行）
MAX_MEMBER_LIST_LINES = 5000


@dataclass(frozen=True)
class ParsedMember:
    """一条有效的成员白名单条目。"""

    user_id: str
    note: str = ""


@dataclass(frozen=True)
class InvalidLine:
    """一条被拒绝的行：保留原始行号与原因，供人工核对后修正文件。"""

    line_no: int
    raw: str
    reason: str


@dataclass(frozen=True)
class MemberListParse:
    """解析结果：有效条目 + 被拒行。"""

    valid: tuple[ParsedMember, ...]
    invalid: tuple[InvalidLine, ...]


def validate_member_id(raw: str) -> str:
    """校验并返回成员标识；不合法抛 ``ValueError``（供单条添加与文件导入共用）。"""
    user_id = str(raw).strip()
    if not user_id:
        raise ValueError("QQ号不能为空")
    if not user_id.isascii() or not user_id.isdigit():
        raise ValueError("QQ号必须是纯数字")
    if not (MIN_MEMBER_ID_LENGTH <= len(user_id) <= MAX_MEMBER_ID_LENGTH):
        raise ValueError(
            f"QQ号长度必须在 {MIN_MEMBER_ID_LENGTH}–{MAX_MEMBER_ID_LENGTH} 位之间（当前 {len(user_id)} 位）"
        )
    return user_id


def parse_member_list(text: str) -> MemberListParse:
    """解析成员白名单文本；非法行不抛出，逐行收集原因（可全部展示给管理员）。"""
    valid: list[ParsedMember] = []
    invalid: list[InvalidLine] = []
    seen: set[str] = set()
    # 容忍 UTF-8 BOM：某些 Windows 编辑器（记事本）会写入 BOM
    lines = text.lstrip("\ufeff").splitlines()
    if len(lines) > MAX_MEMBER_LIST_LINES:
        raise ValueError(f"文件行数超过上限 {MAX_MEMBER_LIST_LINES} 行，已拒绝导入")
    for index, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        body, _, comment = line.partition("#")
        body = body.strip()
        if not body:
            continue
        # 支持 "QQ号,备注"（中英文逗号均可）；备注仅作提示，不参与匹配
        for sep in (",", "，"):
            if sep in body:
                body, _, tail = body.partition(sep)
                body = body.strip()
                comment = f"{tail.strip()}{comment}".strip()
                break
        try:
            user_id = validate_member_id(body)
        except ValueError as exc:
            invalid.append(InvalidLine(line_no=index, raw=raw_line.strip(), reason=str(exc)))
            continue
        if user_id in seen:
            invalid.append(
                InvalidLine(line_no=index, raw=raw_line.strip(), reason="重复的QQ号（已保留首条）")
            )
            continue
        seen.add(user_id)
        valid.append(ParsedMember(user_id=user_id, note=comment.strip()[:MAX_MEMBER_NOTE_LENGTH]))
    return MemberListParse(valid=tuple(valid), invalid=tuple(invalid))


def format_member_list(rows: Iterable[tuple[str, str]]) -> str:
    """把 (QQ号, 备注) 序列格式化为可再导入的文本（与解析器互为逆运算）。"""
    lines = ["# 成员白名单：每行一个QQ号，# 开头为注释", "# 格式：QQ号 或 QQ号,备注"]
    for user_id, note in rows:
        lines.append(f"{user_id},{note}" if note else str(user_id))
    return "\n".join(lines) + "\n"
