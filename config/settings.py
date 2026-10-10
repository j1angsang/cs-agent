from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

REQUIRED_ENV: dict[str, str] = {
    "DB_USER": "PostgreSQL 用户名",
    "DB_PASSWORD": "PostgreSQL 密码",
    "DB_HOST": "PostgreSQL 地址",
    "DB_PORT": "PostgreSQL 端口",
    "DB_NAME": "PostgreSQL 数据库名",
    "SECRET_KEY": "JWT 签名密钥",
    "LLM_BASE_URL": "LLM API Base URL",
    "LLM_API_KEY": "LLM API Key",
    "LLM_MODEL": "LLM 模型名",
}


def missing_settings() -> list[str]:
    return [name for name in REQUIRED_ENV if not (os.getenv(name) or "").strip()]


def validate_settings() -> None:
    from config.actions import config_errors

    errors = [f"- {name}: {REQUIRED_ENV[name]}" for name in missing_settings()]
    errors += config_errors()
    if errors:
        detail = "启动配置校验失败：" + chr(10) + chr(10).join(errors)
        raise RuntimeError(detail)
