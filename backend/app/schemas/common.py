"""公共 DTO 基元：时间序列化与分页。

这些 DTO 是前后端契约的唯一依据：字段名、类型、必填性都在 app/schemas 下定死，
Service 只负责把 Repository 返回的 dict 组装成这里的结构，Router 不做二次改写。

时间约定（与 app/core/time.py 保持一致）：
    项目所有时间以 UTC naive 存库（MySQL DATETIME 不带时区），出参统一补 Z
    变成 ISO 8601 字符串（例如 2025-01-02T03:04:05Z），前端自己转本地时区显示。
"""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, Field, PlainSerializer


def _utc_iso(value: datetime) -> str:
    """naive(UTC) 或 aware datetime -> 带 Z 的 ISO 8601 字符串。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


#: 所有出参时间字段都用它：库里的 naive datetime 出参时自动补 Z
UtcDatetime = Annotated[datetime, PlainSerializer(_utc_iso, return_type=str)]


class PageMeta(BaseModel):
    """分页元信息：所有列表接口共用的 total / limit / offset 语义。"""

    total: int = Field(..., ge=0, description="满足条件的总条数")
    limit: int = Field(..., ge=0, description="本次返回的最大条数")
    offset: int = Field(..., ge=0, description="跳过的条数")
