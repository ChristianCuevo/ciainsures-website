@echo off
REM Double-click to save all changes in this folder and back them up to GitHub.
REM To also publish to the website server later, change PUBLISH_TO_SERVER to 1.
set PUBLISH_TO_SERVER=0
cd /d "%~dp0"
git add -A
git diff --cached --quiet && (echo No new changes to save.) || git commit -q -m "Website update %date% %time%"
echo.
echo Saving to GitHub...
git push origin main
if errorlevel 1 (echo. & echo GITHUB SAVE FAILED - see messages above. & pause & exit /b 1)
echo Saved to GitHub.
if "%PUBLISH_TO_SERVER%"=="1" (
  echo.
  echo Publishing to the website server...
  git push production main
  if errorlevel 1 (echo PUBLISH FAILED - see messages above.) else (echo Published. Changes are live.)
)
echo.
pause
