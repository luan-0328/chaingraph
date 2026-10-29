"""鉴权与用户服务：注册 / 登录 / 个人信息。

规则：
    - 用户名唯一；
    - 注册默认 role=USER（客服/管理员只能由管理员创建或改库，不能让用户自己注册成 ADMIN）；
    - 登录失败不区分"用户不存在"与"密码错误"（防用户名枚举）。
"""

from app.core.config import settings
from app.core.database import session_scope
from app.core.exceptions import AuthenticationError, DuplicateOperationError, ValidationError
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import Role, User, UserStatus
from app.repositories import user_repository

logger = get_logger(__name__)


def register(
    *,
    username: str,
    password: str,
    nickname: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    role: str = Role.USER,
) -> dict:
    """注册用户，返回用户信息（不含密码）。"""
    if role not in Role.ALL:
        raise ValidationError(f"非法角色：{role}")

    with session_scope() as db:
        if user_repository.exists_username(db, username):
            raise DuplicateOperationError(f"用户名 {username} 已被占用", code="USERNAME_TAKEN")

        user = user_repository.create(
            db,
            username=username,
            password_hash=hash_password(password),
            role=role,
            nickname=nickname,
            email=email,
            phone=phone,
        )

    logger.info("注册成功：user_id=%s username=%s role=%s", user["id"], username, role)
    return user


def login(*, username: str, password: str) -> dict:
    """校验账号密码并签发 JWT。"""
    with session_scope() as db:
        user = user_repository.get_by_username(db, username)

    if user is None or not verify_password(password, user.get("password_hash")):
        logger.warning("登录失败：username=%s", username)
        raise AuthenticationError("用户名或密码不正确")

    if user["status"] != UserStatus.ACTIVE:
        raise AuthenticationError("账号已被禁用，请联系管理员")

    token = create_access_token(
        user_id=user["id"], username=user["username"], role=user["role"]
    )
    logger.info("登录成功：user_id=%s role=%s", user["id"], user["role"])

    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": settings.jwt_expire_minutes * 60,
        "user": {key: value for key, value in user.items() if key != "password_hash"},
    }


def get_profile(user_id: int) -> dict:
    """取当前用户资料（**不含 password_hash**，可以安全回给前端）。

    用户不存在时抛 AuthenticationError（401）而不是 404：
    能拿着有效 token 却查不到自己的账号，说明 token 与库里的人已经对不上
    （账号被删/token 伪造），语义上是"认证失效"而不是"资源没找到"。
    """
    with session_scope() as db:
        user = user_repository.get_by_id(db, user_id)
    if user is None:
        raise AuthenticationError("用户不存在")
    return user


def update_profile(
    user_id: int,
    *,
    nickname: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> dict:
    """改个人资料（None = 不改该字段）。

    **只允许改这三个字段**：username 是登录凭据、role 是权限边界，
    都不在这个入口的能力范围内 —— 想改角色得走管理端。
    """
    with session_scope() as db:
        user = user_repository.update_profile(
            db, user_id, nickname=nickname, email=email, phone=phone
        )
    if user is None:
        raise AuthenticationError("用户不存在")

    logger.info("更新个人信息：user_id=%s", user_id)
    return user


def change_password(user_id: int, *, old_password: str, new_password: str) -> None:
    """改密码：先验原密码，再写新哈希。"""
    with session_scope() as db:
        model = db.get(User, user_id)
        if model is None:
            raise AuthenticationError("用户不存在")

        if not verify_password(old_password, model.password_hash):
            raise ValidationError("原密码不正确", code="WRONG_PASSWORD")

        user_repository.update_password(db, user_id, hash_password(new_password))

    logger.info("修改密码成功：user_id=%s", user_id)
