@echo off
REM Double-click to publish: saves all changes in this folder, backs them up to GitHub,
REM and sends them to the live server, which rebuilds the website automatically (about 30 seconds).
cd /d "%~dp0"
git add -A
git diff --cached --quiet && (echo No new changes to save.) || git commit -q -m "Website update %date% %time%"
echo.
echo Backing up to GitHub...
git push origin main
if errorlevel 1 (echo GITHUB BACKUP FAILED - see messages above.)
echo.
echo Publishing to the website server...
git push production main
if errorlevel 1 (echo. & echo PUBLISH FAILED - see messages above.) else (echo. & echo Published. Changes are live.)
pause
