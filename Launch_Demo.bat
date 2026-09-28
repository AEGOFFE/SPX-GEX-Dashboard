@echo off
REM Runs the dashboard in DEMO mode (no Schwab login, no live data).
REM Uses the newest real snapshot in snapshots\ if there is one, otherwise the synthetic sample.
cd /d "%~dp0"
set DEMO_MODE=1
if not exist "snapshots\spx_0dte_*.json" set SNAPSHOT_FILE=sample\sample_snapshot.json
python -m streamlit run app.py
pause
