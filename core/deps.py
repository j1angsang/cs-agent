from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_db
from config.logging_conf import bind_user_id
from core.security import (
    SERVICE_TOKEN_AUDIENCE,
    SERVICE_TOKEN_SCOPE,
    SERVICE_TOKEN_WRITE_SCOPE,
    decode_token_payload,
)
from models import User
from models.user import PRIVILEGED_ROLES, ROLE_LEADER, ROLE_ADMIN

security = HTTPBearer()


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security), db: AsyncSession = Depends(get_db)) -> User:
    # 每次从库里取最新 role，避免改了角色后旧 token 还按旧权限走。
    token = credentials.credentials
    payload = decode_token_payload(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="无效或者过期的token")
    if payload.get("typ") == "service":
        if payload.get("aud") != SERVICE_TOKEN_AUDIENCE or SERVICE_TOKEN_SCOPE not in str(payload.get("scope", "")):
            raise HTTPException(status_code=401, detail="无效的服务凭证")
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="无效或者过期的token")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    bind_user_id(user.id)
    return user


def require_roles(*roles: str):
    async def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(status_code=403, detail="权限不足")
        return current_user
    return checker


async def require_service_write(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    current_user: User = Depends(get_current_user),
) -> User:
    # 服务身份必须带写 scope，只读 token 一律 403，否则读工具瞬间获得写权限。
    payload = decode_token_payload(credentials.credentials)
    if payload and payload.get("typ") == "service" \
            and SERVICE_TOKEN_WRITE_SCOPE not in str(payload.get("scope", "")):
        raise HTTPException(status_code=403, detail="服务凭证缺少写权限")
    return current_user


async def require_service_identity(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    current_user: User = Depends(get_current_user),
) -> User:
    payload = decode_token_payload(credentials.credentials)
    if not payload or payload.get("typ") != "service" \
            or SERVICE_TOKEN_WRITE_SCOPE not in str(payload.get("scope", "")) \
            or payload.get("aud") != SERVICE_TOKEN_AUDIENCE:
        raise HTTPException(status_code=403, detail="该操作仅限客服助手发起")
    return current_user


def actor_type_of(token: str) -> str:
    payload = decode_token_payload(token)
    return "ai" if payload and payload.get("typ") == "service" else "user"


async def get_actor_type(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    return actor_type_of(credentials.credentials)


def can_view_any_user(user: User) -> bool:
    return user.role in PRIVILEGED_ROLES


# 管理端接口只放开给 leader/admin，客服员不推进状态。
require_leader_or_admin = require_roles(ROLE_LEADER, ROLE_ADMIN)
