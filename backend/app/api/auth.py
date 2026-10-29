"""鉴权接口（v2.md §45 Auth）。"""

from fastapi import APIRouter, Depends, status

from app.api import mappers
from app.core.dependencies import get_current_user
from app.memory import user_memory
from app.core.logging import get_logger
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    UpdateProfileRequest,
    UserInfo,
)
from app.services import auth_service

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register", response_model=UserInfo, status_code=status.HTTP_201_CREATED, summary="注册"
)
def register(payload: RegisterRequest) -> UserInfo:
    """注册普通用户（USER）。客服/管理员账号由管理员创建，不能自助注册。"""
    user = auth_service.register(
        username=payload.username,
        password=payload.password,
        nickname=payload.nickname,
        email=payload.email,
        phone=payload.phone,
    )
    return UserInfo(**mappers.user_info(user))


@router.post("/login", response_model=LoginResponse, summary="登录")
def login(payload: LoginRequest) -> LoginResponse:
    """用户名 + 密码 → JWT。"""
    result = auth_service.login(username=payload.username, password=payload.password)
    return LoginResponse(
        access_token=result["access_token"],
        token_type=result["token_type"],
        expires_in=result["expires_in"],
        user=UserInfo(**mappers.user_info(result["user"])),
    )


@router.get("/me", response_model=UserInfo, summary="当前用户")
def me(current_user: dict = Depends(get_current_user)) -> UserInfo:
    return UserInfo(**mappers.user_info(current_user))


@router.get("/me/memories", response_model=list[dict], summary="我的历史处理记忆")
def my_memories(current_user: dict = Depends(get_current_user)) -> list[dict]:
    """查看最多 20 条有效记忆，仅返回当前登录用户的数据。"""
    return user_memory.recall(current_user["id"], limit=20)


@router.delete("/me/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="删除我的一条记忆")
def delete_my_memory(memory_id: int, current_user: dict = Depends(get_current_user)) -> None:
    """从后续记忆检索中删除用户选择的记录，业务层再次检查归属。"""
    user_memory.forget(memory_id, user_id=current_user["id"])


@router.patch("/me", response_model=UserInfo, summary="修改个人信息")
def update_me(
    payload: UpdateProfileRequest,
    current_user: dict = Depends(get_current_user),
) -> UserInfo:
    user = auth_service.update_profile(
        current_user["id"],
        nickname=payload.nickname,
        email=payload.email,
        phone=payload.phone,
    )
    return UserInfo(**mappers.user_info(user))


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT, summary="修改密码")
def change_password(
    payload: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
) -> None:
    auth_service.change_password(
        current_user["id"],
        old_password=payload.old_password,
        new_password=payload.new_password,
    )
