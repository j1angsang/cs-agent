import os

from dotenv import load_dotenv

load_dotenv()

LLM_MODEL = os.getenv("LLM_MODEL", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()

_missing = [
    name
    for name, value in (
        ("LLM_MODEL", LLM_MODEL),
        ("LLM_BASE_URL", LLM_BASE_URL),
        ("LLM_API_KEY", LLM_API_KEY),
    )
    if not value
]
if _missing:
    raise RuntimeError(f"LLM 配置缺失：{', '.join(_missing)}，.env 必填，不提供默认模型名")
