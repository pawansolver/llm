@echo off
echo =========================================
echo Setting up Open WebUI...
echo =========================================

echo.
echo [1/4] Installing Frontend Dependencies...
call npm install

echo.
echo [2/4] Building Frontend...
call npm run build

echo.
echo [3/4] Installing Backend Dependencies...
cd backend
IF NOT EXIST "venv" (
    echo Creating virtual environment with Python 3.12...
    py -3.12 -m venv venv
)
call venv\Scripts\activate.bat
pip install -r requirements.txt

echo.
echo [4/4] Starting the Application...
set FRONTEND_BUILD_DIR=..\build
echo The application will be available at http://localhost:8080
call start_windows.bat
