@echo off
cd /d %~dp0
D:\anaconda3\envs\funAsr\Scripts\pyinstaller.exe funasr.spec --clean
echo.
echo Build complete! Output: dist\funasr.exe
pause
