@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Instalar-Windows.ps1"
if errorlevel 1 (
  echo.
  echo A instalacao nao terminou. Confira a mensagem acima e tente novamente.
  pause
)
endlocal
