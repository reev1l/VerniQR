@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
pushd "%~dp0"
if errorlevel 1 goto path_error
if not exist "verniqr\__main__.py" goto missing_files
if exist ".venv\Scripts\python.exe" goto installed_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 goto use_py
python scripts\install_windows.py
if errorlevel 1 goto failed
goto launch
:use_py
py -3 scripts\install_windows.py
if errorlevel 1 goto failed
goto launch
:installed_python
".venv\Scripts\python.exe" scripts\install_windows.py
if errorlevel 1 goto failed
:launch
".venv\Scripts\python.exe" scripts\prepare_env.py
if errorlevel 1 goto failed
set "VERNIQR_RUN_MODE="
if /I "%~1"=="--web-only" set "VERNIQR_RUN_MODE=--web-only"
".venv\Scripts\python.exe" -m verniqr run %VERNIQR_RUN_MODE%
set "VERNIQR_EXIT_CODE=%errorlevel%"
popd
if not "%VERNIQR_EXIT_CODE%"=="0" pause
exit /b %VERNIQR_EXIT_CODE%
:missing_files
echo Extract the entire ZIP before starting the bot.
goto failed
:path_error
echo Cannot open the project folder. Extract the ZIP to a local folder.
pause
exit /b 1
:failed
echo Setup or startup failed. The error is shown above.
popd
pause
exit /b 1
