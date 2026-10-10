"""工具层请求后端的认证头：按调用者签发短时效服务 token，token 只放 id，不透传用户原始凭据。"""
import logging
import os
import time

from dotenv import load_dotenv

from core.security import (
    SERVICE_TOKEN_SCOPE,
    SERVICE_TOKEN_TTL_MINUTES,
    SERVICE_TOKEN_WRITE_SCOPE,
    create_service_token,
)

load_dotenv()

logger = logging.getLogger(__name__)

# 服务账号 token 从 .env 读，不写死在代码里
SERVICE_TOKEN = os.getenv("SERVICE_TOKEN", "")

# 每用户 token 缓存 (user_id, scope) -> (token, 过期时刻 monotonic)；读写通道分开，最小权限落在 token 上
_token_cache: dict[tuple[int, str], tuple[str, float]] = {}
# 提前 30s 视为过期，避免边界上把刚过期的 token 发出去。
_TOKEN_REFRESH_MARGIN_SECONDS = 30.0

WRITE_CHANNEL_SCOPE = f"{SERVICE_TOKEN_SCOPE} {SERVICE_TOKEN_WRITE_SCOPE}"


def _service_token_for(user_id: int, scope: str) -> str:
    cached = _token_cache.get((user_id, scope))
    now = time.monotonic()
    if cached and cached[1] > now:
        return cached[0]
    token = create_service_token(user_id, scope=scope)
    _token_cache[(user_id, scope)] = (
        token,
        now + SERVICE_TOKEN_TTL_MINUTES * 60 - _TOKEN_REFRESH_MARGIN_SECONDS,
    )
    logger.info("service_token.issued user_id=%s scope=%s ttl_min=%d", user_id, scope, SERVICE_TOKEN_TTL_MINUTES)
    return token


def build_headers(caller: dict | None = None, *, write: bool = False) -> dict:
    """构造 Authorization 头；write=True 时签发带写 scope 的服务 token。"""
    if caller and caller.get("id"):
        scope = WRITE_CHANNEL_SCOPE if write else SERVICE_TOKEN_SCOPE
        return {"Authorization": f"Bearer {_service_token_for(int(caller['id']), scope)}"}
    if SERVICE_TOKEN:
        return {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    return {}
