@echo off
REM Starts Hollow Floor with whichever installed Python has Ursina.
REM Newest Python first; "python" last because on many PCs it is only the
REM Microsoft Store stub, which has no packages installed.
cd /d "%~dp0"
call :find_python
if not defined PY (
    echo Could not find a Python with Ursina installed.
    echo Install Python 3.10 or newer, then in this folder run:
    echo     py -m pip install -r requirements.txt
    pause
    exit /b 1
)
%PY% backrooms.py
if errorlevel 1 pause
exit /b

:find_python
for %%C in ("py -3.14" "py -3.13" "py -3.12" "py -3.11" "py -3.10" "py -3" "python") do (
    %%~C -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('ursina') else 1)" >nul 2>nul && (
        set "PY=%%~C"
        goto :eof
    )
)
goto :eof
