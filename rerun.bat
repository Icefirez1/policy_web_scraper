
@echo off
setlocal

:loop
rem Stop at midnight (local time)
if "%TIME:~0,2%"=="00" goto done

uv run policy-scraper --config config/ijc.yaml collect --run-id 4428b26f61bb41b0a928ea851a4974de

rem Wait 5 seconds before running again
timeout /t 5 /nobreak >nul

goto loop

:done
echo Midnight reached. Stopping.
endlocal
