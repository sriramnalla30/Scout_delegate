@echo off
REM Starts the app on http://localhost:8000 (double-click this file on Windows)

if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
) else (
    echo [!] No virtual environment found - using system Python
)

echo Open http://localhost:8000 in your browser. Press Ctrl+C to stop.
python -m uvicorn app.main:app --reload --port 8000
pause
