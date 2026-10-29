"""时间工具：全项目统一 UTC（naive）存储。

MySQL DATETIME 不保存时区，所以约定：
    写库的时间一律 utcnow()；接口序列化时补 Z，前端负责转本地时区。
"""

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """当前 UTC 时间（naive）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_ago(*, days: float = 0, hours: float = 0, minutes: float = 0) -> datetime:
    """"多久之前"的时间点（造演示数据用）。"""
    return utcnow() - timedelta(days=days, hours=hours, minutes=minutes)


def hours_since(moment: datetime | None) -> float | None:
    """距离某时间点过了多少小时（保留一位小数）。"""
    if moment is None:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return round((utcnow() - moment).total_seconds() / 3600, 1)


def to_iso_utc(moment: datetime | None) -> str | None:
    """序列化成带 Z 的 ISO 8601。"""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
