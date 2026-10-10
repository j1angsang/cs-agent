import os
import uuid
from datetime import datetime, timedelta

import bcrypt
from dotenv import load_dotenv
from jose import jwt, JWTError

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY","dev-secret-key-change-in-production")
ALGORITHMS = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("TOKEN_EXPIRE_MINUTES", "1440"))

SERVICE_TOKEN_AUDIENCE = "customer-service-tools"
SERVICE_TOKEN_SCOPE = "tools:read"
# 写通道额外要求 actions:write，只读 token 调写端点必须 403；不许并入 tools:read。
SERVICE_TOKEN_WRITE_SCOPE = "actions:write"
SERVICE_TOKEN_TTL_MINUTES = int(os.getenv("SERVICE_TOKEN_TTL_MINUTES", "5"))

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False

def create_access_token(user_id:int) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub":str(user_id),"exp":expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHMS)

def create_service_token(user_id: int, scope: str | None = None) -> str:
    now = datetime.utcnow()
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=SERVICE_TOKEN_TTL_MINUTES),
        "aud": SERVICE_TOKEN_AUDIENCE,
        "scope": scope or SERVICE_TOKEN_SCOPE,
        "typ": "service",
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHMS)

def decode_token_payload(token: str) -> dict | None:
    # 这里不验 aud：python-jose 未传 audience 时会对带 aud 的 token 抛 JWTClaimsError，业务校验交给调用方。
    try:
        return jwt.decode(
            token, SECRET_KEY, algorithms=ALGORITHMS, options={"verify_aud": False}
        )
    except JWTError:
        return None

def decode_token(token: str) -> int | None:
    payload = decode_token_payload(token)
    if payload is None:
        return None
    try:
        return int(payload["sub"])
    except (KeyError, ValueError):
        return None