@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Iniciar-Windows.ps1"
if errorlevel 1 (
  echo.
  echo A AURORA nao conseguiu iniciar. Confira a mensagem acima.
  pause
)
endlocal
