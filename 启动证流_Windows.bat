@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
if not exist "%~dp006_运行环境\Windows_x64\python.exe" (
  echo 未找到内置运行环境，请完整解压压缩包后再启动。
  pause
  exit /b 1
)
"%~dp006_运行环境\Windows_x64\python.exe" -X utf8 "%~dp001_程序代码\启动与打包\launcher.py" start
if errorlevel 1 (
  echo 操作未完成，请保留此提示并查看运行数据中的日志。
  pause
  exit /b 1
)
endlocal
