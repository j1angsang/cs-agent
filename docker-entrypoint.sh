#!/bin/sh
# 容器启动先跑 Alembic 迁移，再启动服务。
# 这样新库不是靠 create_all 建表，存量库也通过迁移版本管理。
set -eu
echo "[entrypoint] alembic upgrade head"
alembic upgrade head
echo "[entrypoint] starting uvicorn"
exec "$@"
