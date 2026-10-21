@echo off
chcp 65001 >nul
if not exist "%~dp0start_demo.ps1" goto extract_first
if not exist "%~dp0giveaway\__main__.py" goto extract_first
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_demo.ps1"
pause
exit /b

:extract_first
echo.
echo Демо нельзя запускать отдельным файлом внутри ZIP-архива.
echo Сначала нажмите на архив правой кнопкой мыши и выберите «Извлечь все».
echo Затем откройте распакованную папку vk_giveaway_bot и запустите этот файл снова.
echo.
pause
exit /b 1
