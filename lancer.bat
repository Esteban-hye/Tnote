@echo off
cd /d "%~dp0"
for /f "tokens=*" %%P in ('python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set PYW=%%P
start "" "%PYW%" "%~dp0tnote.py"
