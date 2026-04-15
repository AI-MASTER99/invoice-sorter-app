@echo off
echo ================================================
echo   Invoice Sorter — Code naar GitHub pushen
echo ================================================
echo.

cd /d C:\InvoiceFlow

echo Stap 1: Git initialiseren...
git init
git branch -M main

echo.
echo Stap 2: GitHub koppelen...
git remote remove origin 2>nul
git remote add origin https://github.com/AI-MASTER99/invoice-sorter.com.git

echo.
echo Stap 3: Bestanden toevoegen...
git add .

echo.
echo Stap 4: Commit aanmaken...
git commit -m "Invoice Sorter - initieel"

echo.
echo Stap 5: Naar GitHub pushen...
git push -u origin main

echo.
echo ================================================
if %ERRORLEVEL% EQU 0 (
    echo   KLAAR! Code staat nu op GitHub.
    echo   Vercel deploy begint automatisch.
) else (
    echo   Er ging iets mis. Zie foutmelding hierboven.
)
echo ================================================
echo.
pause
