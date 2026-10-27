@echo off
REM 公共剧本库增量刷新（每 3 天由 schtasks 调用，见根 README「公共剧本库」小节）。
REM 流程：增量采集（前 5 页 + 水位线）→ dry-run 预览 → 正式导入 → 摘要入日志。
REM 任一步失败以非零码退出。日志只记计数与日期，不含 DATABASE_URL/key。
chcp 65001 >nul
setlocal

cd /d "%~dp0.."

set "PYTHONUTF8=1"
set "LOG=data\refresh_log.txt"

echo [%date% %time%] refresh start >> "%LOG%"

python scripts\fetch_vshang66.py --refresh >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] refresh FAILED at fetch stage >> "%LOG%"
    exit /b 1
)

python -m app.store.import_public_scripts data\public_scripts_vshang66.json --dry-run >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] refresh FAILED at dry-run stage >> "%LOG%"
    exit /b 1
)

python -m app.store.import_public_scripts data\public_scripts_vshang66.json >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] refresh FAILED at import stage >> "%LOG%"
    exit /b 1
)

echo [%date% %time%] refresh done >> "%LOG%"
exit /b 0
