
@echo off
setlocal

:loop
rem Stop at midnight (local time)
if "%TIME:~0,2%"=="00" goto done
if "%TIME:~0,2%"==" 0" goto done

uv run policy-scraper --config config/ijc.yaml collect-known --run-id 4428b26f61bb41b0a928ea851a4974de
if errorlevel 3 goto done
if errorlevel 1 if not errorlevel 2 goto done
powershell -NoProfile -Command "try { $s = Get-Content -Raw 'data/runs/4428b26f61bb41b0a928ea851a4974de/invocation.json' | ConvertFrom-Json; if ($s.continue_batch) { exit 0 }; exit 1 } catch { exit 1 }"
if errorlevel 1 goto done

rem Wait 5 seconds before running again
timeout /t 5 /nobreak >nul

goto loop

:done
echo Batch stopped. Inspect invocation.json for the stop reason.
endlocal
