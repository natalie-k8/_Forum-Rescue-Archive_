@echo off
chcp 65001 >nul
REM Собирает Forum Rescue Archive в один .exe-файл.
REM Запускать из папки проекта (там, где main.py).

echo Устанавливаю PyInstaller...
py -m pip install pyinstaller
if errorlevel 1 (
    echo.
    echo ОШИБКА: не удалось установить PyInstaller — смотри сообщение выше.
    pause
    exit /b 1
)

echo.
echo Собираю .exe...
py -m PyInstaller --onefile --console --name ForumRescueArchive main.py
if errorlevel 1 (
    echo.
    echo ОШИБКА: PyInstaller завершился с ошибкой — смотри сообщение выше.
    pause
    exit /b 1
)

if not exist "dist\ForumRescueArchive.exe" (
    echo.
    echo ОШИБКА: файл dist\ForumRescueArchive.exe не появился, хотя PyInstaller
    echo не сообщил об ошибке. Пришли весь вывод выше.
    pause
    exit /b 1
)

echo.
echo Готово. Файл: dist\ForumRescueArchive.exe
echo.
echo ВАЖНО: скопируй рядом с .exe (в ту же папку, куда его положишь для
echo постоянного использования) файлы .env, .env.forum2x2, config.json,
echo config_forum2x2.json и папки Archive/, Archive_forum2x2/ — программа
echo ищет их рядом с тем местом, откуда её запустили, а не внутри .exe.
pause
