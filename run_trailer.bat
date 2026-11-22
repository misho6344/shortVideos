@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "movie.mp4" (
  echo movie.mp4 ვერ მოიძებნა. დააკოპირე ფილმის ფაილი ამ საქაღალდეში.
  pause
  exit /b 1
)

python3 make_trailer.py "movie.mp4" -o trailer.mp4 --duration 170 --title "კრიმინალი ემილი" --font "C:\Windows\Fonts\sylfaen.ttf" --font-name "Sylfaen"

if errorlevel 1 (
  echo.
  echo შეცდომა მოხდა. იხილე ზემოთ მოცემული ტექსტი.
) else (
  echo.
  echo მზადაა: trailer.mp4
)
pause
