"""鉴权与用户 DTO（v2.md §3 认证、§4 用户）。

这些 DTO 是前后端契约的唯一依据。

命名约定：DTO 里主键统一带业务前缀（user_id / product_id / order_id ...），
而 ORM 的 to_dict() 里主键就叫 id —— 由 Service 层负责改名，Schema 不依赖 ORM。
"""

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime


class RegisterRequest(BaseModel):
    """POST /auth/register：注册请求体（对应 UserService.register）。"""

    username: str = Field(..., min_length=3, max_length=64, description="登录名，全局唯一")
    password: str = Field(..., min_length=6, max_length=128)
    nickname: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=128)
    phone: str | None = Field(default=None, max_length=32)


class LoginRequest(BaseModel):
    """POST /auth/login：登录请求体。

    这里 password 只要求非空：格式规则属于注册时的校验，
    登录时密码不符应当由服务层返回 401（UNAUTHORIZED），而不是 422。
    """

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class UserInfo(BaseModel):
    """用户信息 DTO（注册/登录/个人资料接口的出参）。永远不含 password_hash。"""

    user_id: int
    username: str
    nickname: str | None = None
    email: str | None = None
    phone: str | None = None
    role: str
    created_at: UtcDatetime


class LoginResponse(BaseModel):
    """POST /auth/login 出参：JWT 票据 + 当前用户信息。"""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int = Field(..., gt=0, description="令牌有效期（秒）")
    user: UserInfo


class UpdateProfileRequest(BaseModel):
    """PUT /users/me：更新个人资料。

    三个字段都可选，且 None 表示"不修改该字段"（不是清空），
    服务层用 model_dump(exclude_unset=True) 区分"没传"与"传了 null"。
    """

    nickname: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=128)
    phone: str | None = Field(default=None, max_length=32)


class ChangePasswordRequest(BaseModel):
    """PUT /users/me/password：修改密码（需校验原密码）。"""

    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=128)
