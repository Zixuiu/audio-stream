@echo off
echo ========================================
echo 启动手机音频投屏
echo ========================================
echo.
echo 请确保：
echo 1. 手机已通过USB连接到电脑
echo 2. 手机已开启USB调试
echo.
echo 正在启动 scrcpy...
echo.

scrcpy --audio-codec=aac --stay-awake -m 1024

pause
