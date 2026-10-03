@echo off
setlocal
cd /d "%~dp0"
title Streamforge Tiny2 UserControl v0.4.6 - MANUAL

echo ========================================
echo  Streamforge Tiny2 UserControl v0.4.6 - MANUAL RECOVERY
echo ========================================
echo.

where py >nul 2>nul
if not errorlevel 1 (
    py -3 tiny2_camera_v0.4.6.py
    goto :after
)

where python >nul 2>nul
if not errorlevel 1 (
    python tiny2_camera_v0.4.6.py
    goto :after
)

echo [ERROR] Python 3 が見つかりません。
echo Python 3 をインストールしてから再実行してください。
echo.
pause
exit /b 1

:after
if errorlevel 1 (
    echo.
    echo [ERROR] controller が異常終了しました。
    pause
)
endlocal
