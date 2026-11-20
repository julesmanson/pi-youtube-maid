@echo off
REM Closes all running Chrome windows, then relaunches Chrome with remote
REM debugging enabled so youtube_maid.pyw can attach to it.
REM Chrome only accepts --remote-debugging-port on a fresh start, so the
REM taskkill step is required, not optional - you will lose any unsaved
REM state in open tabs when this runs (closed tabs come back via "restore
REM previous session" when Chrome reopens, same as after any Chrome restart).

set CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
set CHROME_PATH_PERUSER=%LocalAppData%\Google\Chrome\Application\chrome.exe
set MAID_PATH=%~dp0youtube_maid.pyw

REM Chrome installs system-wide (first path) or per-user (second) depending on
REM how it was installed - checking only one is a real fragility risk if this
REM ever runs after a reinstall or on another machine.
if not exist "%CHROME_PATH%" set CHROME_PATH=%CHROME_PATH_PERUSER%

if not exist "%CHROME_PATH%" (
    echo ERROR: Chrome not found at either common install location.
    echo Edit this .bat file and fix CHROME_PATH if Chrome is installed elsewhere.
    pause
    exit /b 1
)

if not exist "%MAID_PATH%" (
    echo ERROR: youtube_maid.pyw not found next to this .bat file.
    echo Make sure both files stay in the same folder.
    pause
    exit /b 1
)

taskkill /IM chrome.exe /F >nul 2>&1
timeout /T 2 /NOBREAK >nul

start "" "%CHROME_PATH%" --remote-debugging-port=9222
timeout /T 5 /NOBREAK >nul
start "" "%MAID_PATH%"

exit /b 0
