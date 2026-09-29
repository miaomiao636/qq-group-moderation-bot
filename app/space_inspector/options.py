"""Explicit per-run desktop settings; none changes moderation policy."""

from dataclasses import dataclass

from .contracts import InspectionError


@dataclass(frozen=True)
class ScanOptions:
    continuous: bool = True
    delay_seconds: int = 30
    batch_size: int = 300
    batch_pause_seconds: int = 60
    reuse_hours: int = 24
    lightweight: bool = True
    defer_qq: str = ""

    def validate(self) -> None:
        for value, low, high in (
            (self.delay_seconds, 5, 300),
            (self.batch_size, 1, 300),
            (self.batch_pause_seconds, 30, 3600),
            (self.reuse_hours, 0, 24),
        ):
            if type(value) is not int or not low <= value <= high:
                raise InspectionError("巡检设置超出支持范围，请检查间隔、批次和历史有效期。")
        if type(self.continuous) is not bool or type(self.lightweight) is not bool:
            raise InspectionError("巡检设置格式无效。")
        if self.defer_qq:
            from .contracts import numeric_id

            numeric_id(self.defer_qq)
