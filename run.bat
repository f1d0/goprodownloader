@echo off
REM Convenience launcher. Double-click me.
cd /d "%~dp0"
python gopro_downloader.py --har gopro.com.har --out GoProLibrary --workers 2 %*
echo.
pause
