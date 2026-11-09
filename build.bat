@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo [1/3] 安装依赖...
python -m pip install -r requirements.txt || goto :error
python -m pip install pyinstaller || goto :error

echo [2/3] 打包 QQAudio2MP3.exe ...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name QQAudio2MP3 main.py || goto :error

echo [3/3] 清理中间文件...
if exist build rmdir /s /q build
if exist QQAudio2MP3.spec del /q QQAudio2MP3.spec

echo.
echo 打包完成：%~dp0dist\QQAudio2MP3.exe
exit /b 0

:error
echo.
echo 打包失败，请检查上面的错误信息。
exit /b 1
