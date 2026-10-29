"""用户数据访问。

约定（整个 repositories 包统一）：
    - 第一个参数是 db: Session（事务由 Service 层的 session_scope 控制）；
    - 只往外返回 dict，不返回 ORM 对象；
    - 查不到返回 None，由 Service 决定抛什么业务异常。
"""

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.user import User


def create(
    db: Session,
    *,
    username: str,
    password_hash: str,
    role: str = "USER",
    nickname: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> dict:
    """新建用户。

    `password_hash` 是**已经算好的哈希**，不是明文 ——
    哈希算法在 core/security.py，仓储不碰密码学。
    `nickname` 缺省取 username：前端到处要显示"昵称"，为空会让界面出现空洞。

    role 默认 USER。客服/管理员角色只能由管理员设置或直接改库，
    **绝不允许用户注册时自己指定**（那是个提权漏洞）。
    """
    user = User(
        username=username,
        password_hash=password_hash,
        role=role,
        nickname=nickname or username,
        email=email,
        phone=phone,
    )
    db.add(user)
    db.flush()
    db.refresh(user)
    return user.to_dict()


def get_by_id(db: Session, user_id: int) -> dict | None:
    """按主键取用户（**不含 password_hash**，可以安全地返回给前端）。"""
    user = db.get(User, user_id)
    return user.to_dict() if user else None


def lock_cart_owner(db: Session, user_id: int) -> None:
    """串行化同一用户的购物车变更与结算，包括购物车尚无行的情况。"""
    from app.core.exceptions import UserNotFoundError
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise UserNotFoundError("用户不存在")


def get_by_username(db: Session, username: str) -> dict | None:
    """登录用：**带 password_hash**，只给 auth_service 用。"""
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        return None
    return {**user.to_dict(), "password_hash": user.password_hash}


def exists_username(db: Session, username: str) -> bool:
    """用户名是否已被占用（注册时用 COUNT 而不是查整行，少传一次数据）。"""
    return db.scalar(select(func.count()).select_from(User).where(User.username == username)) > 0


def update_profile(
    db: Session,
    user_id: int,
    *,
    nickname: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> dict | None:
    """改资料（None = 不改该字段）。

    **改不了 username 和 role** —— 这两个字段不在参数里是刻意的：
    用户名是登录凭据、角色是权限边界，都不该由"改资料"这个入口动。
    """
    user = db.get(User, user_id)
    if user is None:
        return None

    if nickname is not None:
        user.nickname = nickname
    if email is not None:
        user.email = email
    if phone is not None:
        user.phone = phone

    db.flush()
    db.refresh(user)
    return user.to_dict()


def update_password(db: Session, user_id: int, password_hash: str) -> dict | None:
    """改密码（传入的同样是哈希后的值）。

    单独一个方法而不是塞进 update_profile：改密码要求"先验旧密码"
    （见 auth_service），它是一条独立的安全路径，不该和改昵称共用一个入口。
    """
    user = db.get(User, user_id)
    if user is None:
        return None
    user.password_hash = password_hash
    db.flush()
    return user.to_dict()


def list_users(
    db: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    keyword: str | None = None,
    role: str | None = None,
) -> tuple[list[dict], int]:
    """管理端用户列表。"""
    conditions = []
    if role:
        conditions.append(User.role == role)
    if keyword:
        like = f"%{keyword}%"
        conditions.append(or_(User.username.like(like), User.nickname.like(like)))

    count_stmt = select(func.count()).select_from(User)
    list_stmt = select(User)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(list_stmt.order_by(User.id.desc()).limit(limit).offset(offset))
    return [user.to_dict() for user in rows], total
