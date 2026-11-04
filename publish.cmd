@echo off
REM Double-click to publish: saves all changes in this folder and sends them to the live server,
REM which rebuilds and updates the website automatically (about 30 seconds).
cd /d "%~dp0"
git add -A
git diff --cached --quiet && (echo No changes to publish.) || git commit -q -m "Website update %date% %time%"
git push production main
if errorlevel 1 (echo. & echo PUBLISH FAILED - see messages above.) else (echo. & echo Published. Changes are live.)
pause
