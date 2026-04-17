@echo off
cd /d "%~dp0"
echo Starting Azure Sponsorship Monitor...
start "Azure Sponsorship Monitor" cmd /k ".venv\Scripts\activate.bat && python -m flask run"
timeout /t 2 /nobreak > nul
start "" "http://127.0.0.1:5000"
