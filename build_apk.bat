@echo off
echo =========================================
echo Building Open WebUI Mobile App (APK)
echo =========================================

echo.
echo [1/3] Building Frontend (This may take a few minutes)...
set NODE_OPTIONS=--max-old-space-size=8192
call npm run build
if %errorlevel% neq 0 (
    echo Frontend build failed!
    pause
    exit /b %errorlevel%
)

echo.
echo [2/3] Syncing Capacitor files to Android project...
call npx cap sync android
if %errorlevel% neq 0 (
    echo Capacitor sync failed!
    pause
    exit /b %errorlevel%
)

echo.
echo [3/3] Compiling Android APK...
set JAVA_HOME=C:\AndroidSDK\jdk21
set ANDROID_HOME=C:\AndroidSDK
cd android
call gradlew assembleDebug
cd ..

if %errorlevel% neq 0 (
    echo Android build failed!
    pause
    exit /b %errorlevel%
)

echo.
echo =========================================
echo BUILD SUCCESSFUL!
echo =========================================
echo Your APK file is ready at:
echo android\app\build\outputs\apk\debug\app-debug.apk
echo.
pause
