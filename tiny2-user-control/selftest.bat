@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 tiny2_camera_v0.4.6.py --selftest
    goto :done
)
where python >nul 2>nul
if not errorlevel 1 (
    python tiny2_camera_v0.4.6.py --selftest
    goto :done
)
echo [ERROR] Python 3 が見つかりません。
:done
pause
endlocal
