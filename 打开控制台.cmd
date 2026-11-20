@echo off
cd /d "%~dp0"
for %%F in (*.pyw) do (
    start "" pythonw.exe "%%~fF"
    exit /b
)
