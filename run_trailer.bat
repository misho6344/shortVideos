@echo off
chcp 65001 >nul
cd /d "%~dp0"

python3 make_trailer.py -o trailer.mp4 --duration 170 --font "C:\Windows\Fonts\sylfaen.ttf" --font-name "Sylfaen"

if errorlevel 1 (
  echo.
  echo Error. See the messages above.
) else (
  echo.
  echo Done: trailer.mp4
)
pause
