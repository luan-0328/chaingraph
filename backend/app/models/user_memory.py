"""用户长期记忆（v2.md §41.2）。

V1 不做复杂的记忆算法：只把"值得跨工单记住的用户事实"结构化存下来
（例如"该用户 30 天内已申请过 3 次退款"这类可查询的事实），
需要时由 Agent 读取，而不是把历史对话全塞进上下文。
"""

from sqlalchemy import BigInteger, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class MemoryType:
    FACT = "FACT"          # 事实（偏好、习惯）
    SUMMARY = "SUMMARY"    # 历史工单摘要
    NOTE = "NOTE"          # 客服备注


class UserMemory(Base, TimestampMixin):
    __tablename__ = "user_memories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(16), nullable=False, default=MemoryType.FACT)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("idx_user_memories_user_id", "user_id"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "memory_type": self.memory_type,
            "content": self.content,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def __repr__(self) -> str:
        return f"<UserMemory user={self.user_id} type={self.memory_type}>"
