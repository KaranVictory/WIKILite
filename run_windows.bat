@echo off
setlocal
cd /d "%~dp0"

rem Find either the Windows Python launcher (py) or the Python command.
set "PYTHON_CMD="
where py >nul 2>nul && set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
  where python >nul 2>nul && set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD goto python_missing

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PYTHON_CMD% -m venv .venv
  if errorlevel 1 goto venv_failed
)

echo Installing required packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto install_failed

echo.
echo Server starting. Open http://127.0.0.1:5000 in your browser.
echo Stop the server with Ctrl+C.
echo.
".venv\Scripts\python.exe" app.py
goto end

:python_missing
echo Python was not found on PATH.
echo Open Command Prompt and run: python --version
 echo If that command also fails, reinstall Python and enable "Add python.exe to PATH".
goto fail

:venv_failed
echo Could not create the virtual environment.
echo Check that your Python installation includes the venv and pip modules.
goto fail

:install_failed
echo Package installation failed.
echo Check your internet connection, then try again.
echo If the error mentions a compiler or unsupported wheel, share the full error text.
goto fail

:fail
echo.
pause
exit /b 1

:end
endlocal
