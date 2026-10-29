"""用户表 + 角色。

v2.md §2 定义三个角色：
    USER              普通用户（购物、下单、售后、工单）
    CUSTOMER_SERVICE  客服（工单处理、审批、接管）
    ADMIN             管理员（商品、知识库、全量数据）
V1 不做复杂 RBAC，就这三个。
"""

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class Role:
    """用户角色（与 v2.md §2 一致）。"""

    USER = "USER"
    CUSTOMER_SERVICE = "CUSTOMER_SERVICE"
    ADMIN = "ADMIN"

    ALL = (USER, CUSTOMER_SERVICE, ADMIN)
    STAFF = (CUSTOMER_SERVICE, ADMIN)   # 内部人员


class UserStatus:
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=Role.USER)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=UserStatus.ACTIVE)

    __table_args__ = (Index("idx_users_role", "role"),)

    def to_dict(self) -> dict:
        """注意：永远不包含 password_hash。"""
        return {
            "id": self.id,
            "username": self.username,
            "nickname": self.nickname,
            "email": self.email,
            "phone": self.phone,
            "role": self.role,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} role={self.role}>"
