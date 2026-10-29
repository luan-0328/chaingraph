"""模型公共部分：时间戳 Mixin + 序列化助手。

约定：
    - 所有模型都有 created_at / updated_at（UTC，Python 侧生成）；
    - 每个模型都有 to_dict()，Repository 只往外返回 dict（不返回 ORM 对象）；
    - 金额统一 Decimal(10,2) 存库，to_dict() 时转 float（方便 JSON / 给 LLM 看）。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow


class TimestampMixin:
    """创建/更新时间。"""

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


def money(value: Decimal | float | int | None) -> float | None:
    """金额转 float（对外 JSON 与工具返回值都用它）。"""
    return float(value) if value is not None else None


def dt(value: datetime | None) -> datetime | None:
    """透传（保留 datetime 类型，交给 Pydantic 序列化成带 Z 的 ISO）。"""
    return value
