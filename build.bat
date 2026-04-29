@echo off
setlocal
cd /d "%~dp0"

echo [SAMP-Translate] Checking virtual environment...
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run "python -m venv .venv" first.
    pause
    exit /b 1
)

echo [SAMP-Translate] Installing dependencies...
.venv\Scripts\pip.exe install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo [SAMP-Translate] Checking PyInstaller...
.venv\Scripts\python.exe -m PyInstaller --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing PyInstaller into venv...
    .venv\Scripts\pip.exe install pyinstaller
    if %errorlevel% neq 0 (
        echo ERROR: Failed to install PyInstaller.
        pause
        exit /b 1
    )
)

echo [SAMP-Translate] Locating Tcl/Tk...
for /f "delims=" %%i in ('.venv\Scripts\python.exe -c "import sys; print(sys.base_prefix)"') do set PYTHON_BASE=%%i
set TCL_LIBRARY=%PYTHON_BASE%\tcl\tcl8.6
set TK_LIBRARY=%PYTHON_BASE%\tcl\tk8.6

echo [SAMP-Translate] Building...
.venv\Scripts\pyinstaller.exe SAMP-Translate.spec --clean --noconfirm

if %errorlevel% equ 0 (
    echo [SAMP-Translate] Cleaning up build folder...
    rmdir /s /q build

    echo.
    echo Build complete: dist\SAMP-Translate\SAMP-Translate.exe
    echo To distribute: zip the dist\SAMP-Translate\ folder and share it.
    echo.
    explorer dist\SAMP-Translate
) else (
    echo.
    echo Build failed. Check the output above for errors.
    pause
    exit /b 1
)
