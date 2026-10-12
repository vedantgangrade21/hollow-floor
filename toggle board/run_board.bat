@echo off
REM Starts the horror effects test board. It only needs Python's standard
REM library, so any real Python works ("python" may just be the Store stub).
cd /d "%~dp0"
for %%C in ("py -3" "python") do (
    %%~C -c "import tkinter" >nul 2>nul && (
        %%~C horror_toggle_board.py
        if errorlevel 1 pause
        exit /b
    )
)
echo Could not find Python. Install Python 3.10 or newer from python.org.
pause
exit /b 1
