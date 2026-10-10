@echo off
chcp 65001 >nul
cd /d %~dp0
echo ========================================
echo   客服系统启动（http://127.0.0.1:8000）
echo   接口文档：http://127.0.0.1:8000/docs
echo ========================================
set PYTHONIOENCODING=utf-8
REM schema 由 Alembic 管理，启动前先迁移（幂等）
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
echo.
echo 服务已停止。
pause
