@echo off
chcp 65001 > nul
setlocal EnableDelayedExpansion

echo.
echo ╔══════════════════════════════════════╗
echo ║       FlightBot Scout — Tests        ║
echo ╚══════════════════════════════════════╝
echo.

:: ── Определяем папку скрипта (корень проекта flighbot_scout) ──────────────────
set "PROJECT=%~dp0"
:: Убираем trailing slash
if "%PROJECT:~-1%"=="\" set "PROJECT=%PROJECT:~0,-1%"

echo [1/3] Корень проекта: %PROJECT%

:: ── Ищем python.exe: сначала venv рядом, потом venv в соседней папке ─────────
set "PYTHON="

:: Вариант 1: venv прямо в папке проекта
if exist "%PROJECT%\venv\Scripts\python.exe" (
    set "PYTHON=%PROJECT%\venv\Scripts\python.exe"
    echo [2/3] venv найден: %PROJECT%\venv
    goto :found_python
)

:: Вариант 2: venv в соседней папке (как у тебя: "ТЕСТ WOW Bilet\venv")
for /d %%D in ("%PROJECT%\..\*") do (
    if exist "%%D\venv\Scripts\python.exe" (
        set "PYTHON=%%D\venv\Scripts\python.exe"
        echo [2/3] venv найден: %%D\venv
        goto :found_python
    )
)

:: Вариант 3: системный python
where python >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=python"
    echo [2/3] Используется системный python
    goto :found_python
)

echo [ОШИБКА] Python не найден. Активируй venv вручную и запусти снова.
pause
exit /b 1

:found_python
echo.

:: ── Проверяем / устанавливаем pytest ─────────────────────────────────────────
echo [3/3] Проверка pytest...
"%PYTHON%" -m pytest --version >nul 2>&1
if %errorlevel% neq 0 (
    echo      pytest не найден — устанавливаю...
    "%PYTHON%" -m pip install pytest pytest-asyncio --quiet
    if %errorlevel% neq 0 (
        echo [ОШИБКА] Не удалось установить pytest.
        pause
        exit /b 1
    )
    echo      pytest установлен ✓
) else (
    for /f "tokens=*" %%V in ('"%PYTHON%" -m pytest --version 2^>^&1') do echo      %%V
)

echo.
echo ════════════════════════════════════════
echo   Запуск тестов...
echo ════════════════════════════════════════
echo.

:: ── Запуск: из корня проекта чтобы pytest.ini подхватился ────────────────────
cd /d "%PROJECT%"

:: Если передан аргумент — запускаем конкретный файл: run_tests.bat test_search_flow.py
if "%~1"=="" (
    "%PYTHON%" -m pytest test\ -v --tb=short
) else (
    "%PYTHON%" -m pytest "test\%~1" -v --tb=short
)

set "EXIT=%errorlevel%"
echo.
if %EXIT%==0 (
    echo ✅ Все тесты прошли успешно!
) else (
    echo ❌ Есть упавшие тесты. Код выхода: %EXIT%
)

echo.
pause
exit /b %EXIT%