# Python 3.13-slim + uv 装依赖，非 root 运行
# 两个基础镜像都可用 build-arg 覆盖，方便国内网络换源
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.26
ARG PYTHON_IMAGE=python:3.13-slim

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# 直接用官方 uv 镜像里的二进制，不在镜像里 pip 裸装
COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

COPY . .

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chmod +x /app/docker-entrypoint.sh \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
