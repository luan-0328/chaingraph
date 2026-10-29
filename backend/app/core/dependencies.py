"""FastAPI 依赖：当前用户 + 角色校验。

v2.md §58 要求："权限不能只依靠前端路由隐藏，后端必须再次验证。"

用法：

    # 任意登录用户
    def create_order(payload: OrderCreate, user = Depends(get_current_user)): ...

    # 只有客服 / 管理员
    @router.get("/admin/orders")
    def admin_orders(user = Depends(require_roles(Role.CUSTOMER_SERVICE, Role.ADMIN))): ...

    # 用户和客服都行，但业务里再判断"是不是自己的"
    def get_order(order_id: int, user = Depends(get_current_user)): ...
"""

from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.database import session_scope
from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger
from app.core.security import decode_access_token
from app.repositories import user_repository
from app.models.user import UserStatus

logger = get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False, description="登录返回的 access_token")


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    token: str | None = Query(
        default=None,
        description="令牌（浏览器 EventSource 无法自定义请求头时用它订阅 SSE）",
    ),
) -> dict:
    """解析令牌 → 回库确认用户仍存在 → 返回用户 dict（含 role）。"""
    raw_token = credentials.credentials if credentials else token

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已失效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(raw_token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期，请重新登录"
        ) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录凭证无效，请重新登录"
        ) from exc

    try:
        user_id = int(payload.get("sub"))
        if user_id <= 0:
            raise ValueError("invalid subject")
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录凭证无效") from exc

    with session_scope() as db:
        user = user_repository.get_by_id(db, user_id)

    if user is None or user["status"] != UserStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已被禁用")

    return user


def require_roles(*roles: str) -> Callable[..., dict]:
    """依赖工厂：只有指定角色能访问。

    角色以**数据库里的当前值**为准（每次请求都会查用户），
    所以管理员改了某人角色后，旧 token 也会立即失去/获得权限。
    """
    allowed = set(roles)

    def checker(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user["role"] not in allowed:
            logger.warning(
                "权限不足：user_id=%s role=%s 访问需要 %s 的接口",
                current_user["id"],
                current_user["role"],
                "/".join(sorted(allowed)),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="暂无权限执行该操作",
            )
        return current_user

    return checker


__all__ = [
    "AuthenticationError",
    "get_current_user",
    "require_roles",
]
