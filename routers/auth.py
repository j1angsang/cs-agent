import asyncio
import logging
import os
import time
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.params import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import get_db
from core.deps import get_current_user
from core.rate_limit import create_rate_limiter
from core.security import hash_password, verify_password, create_access_token
from models import User

router = APIRouter(prefix="/auth", tags=["认证"])
logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


# 账号维度挡定向爆破，IP 维度挡批量撞库/注册
LOGIN_ACCOUNT_LIMITER = create_rate_limiter(
    max_requests=_env_int("AUTH_LOGIN_ACCOUNT_MAX", 10),
    window_seconds=_env_int("AUTH_LOGIN_WINDOW_SECONDS", 60),
)
LOGIN_IP_LIMITER = create_rate_limiter(
    max_requests=_env_int("AUTH_LOGIN_IP_MAX", 30),
    window_seconds=_env_int("AUTH_LOGIN_WINDOW_SECONDS", 60),
)
REGISTER_ACCOUNT_LIMITER = create_rate_limiter(
    max_requests=_env_int("AUTH_REGISTER_ACCOUNT_MAX", 5),
    window_seconds=_env_int("AUTH_REGISTER_WINDOW_SECONDS", 300),
)
REGISTER_IP_LIMITER = create_rate_limiter(
    max_requests=_env_int("AUTH_REGISTER_IP_MAX", 10),
    window_seconds=_env_int("AUTH_REGISTER_WINDOW_SECONDS", 300),
)


def _raise_429(retry_after: int, action: str) -> None:
    from core.metrics import RATE_LIMIT_REJECTED_TOTAL

    RATE_LIMIT_REJECTED_TOTAL.labels(scope="auth").inc()
    raise HTTPException(
        status_code=429,
        detail=f"{action}过于频繁，请 {retry_after} 秒后再试",
        headers={"Retry-After": str(retry_after)},
    )


async def _check_login_limits(request: Request, username: str) -> None:
    ip = _client_ip(request)
    retry_after = await LOGIN_ACCOUNT_LIMITER.check(f"login:user:{username.strip().lower()}")
    if retry_after is not None:
        logger.warning("auth.login_rate_limited dimension=account username=%s ip=%s retry_after=%s", username, ip, retry_after)
        _raise_429(retry_after, "登录尝试")
    retry_after = await LOGIN_IP_LIMITER.check(f"login:ip:{ip}")
    if retry_after is not None:
        logger.warning("auth.login_rate_limited dimension=ip username=%s ip=%s retry_after=%s", username, ip, retry_after)
        _raise_429(retry_after, "登录尝试")


async def _check_register_limits(request: Request, username: str) -> None:
    ip = _client_ip(request)
    retry_after = await REGISTER_ACCOUNT_LIMITER.check(f"register:user:{username.strip().lower()}")
    if retry_after is not None:
        logger.warning("auth.register_rate_limited dimension=account username=%s ip=%s retry_after=%s", username, ip, retry_after)
        _raise_429(retry_after, "注册请求")
    retry_after = await REGISTER_IP_LIMITER.check(f"register:ip:{ip}")
    if retry_after is not None:
        logger.warning("auth.register_rate_limited dimension=ip username=%s ip=%s retry_after=%s", username, ip, retry_after)
        _raise_429(retry_after, "注册请求")


def reset_auth_rate_limiters() -> None:
    async def _reset_all():
        for limiter in (LOGIN_ACCOUNT_LIMITER, LOGIN_IP_LIMITER, REGISTER_ACCOUNT_LIMITER, REGISTER_IP_LIMITER):
            await limiter.reset()

    asyncio.run(_reset_all())


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "username": current_user.username, "role": current_user.role}


@router.post("/register")
async def register(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        db: AsyncSession = Depends(get_db)
):
    await _check_register_limits(request, username)
    started = time.perf_counter()
    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none():
        logger.info("auth.register_rejected username=%s reason=duplicate elapsed_ms=%.1f", username, (time.perf_counter() - started) * 1000)
        raise HTTPException(status_code=400, detail="用户名已存在")
    user = User(username=username, hashed_password=hash_password(password))
    db.add(user)
    await db.flush()
    await db.commit()
    logger.info("auth.register_ok user_id=%s username=%s elapsed_ms=%.1f", user.id, user.username, (time.perf_counter() - started) * 1000)
    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/login")
async def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        db: AsyncSession = Depends(get_db)
):
    await _check_login_limits(request, username)
    started = time.perf_counter()
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(password, user.hashed_password):
        logger.warning(
            "auth.login_failed username=%s ip=%s reason=%s elapsed_ms=%.1f",
            username, _client_ip(request), "user_not_found_or_bad_password",
            (time.perf_counter() - started) * 1000,
        )
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    user.last_login_at = datetime.now()
    await db.commit()
    token = create_access_token(user.id)
    logger.info(
        "auth.login_ok user_id=%s username=%s ip=%s elapsed_ms=%.1f",
        user.id, user.username, _client_ip(request), (time.perf_counter() - started) * 1000,
    )
    return {"access_token": token, "token_type": "bearer", "role": user.role}
