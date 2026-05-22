@echo off
chcp 65001 >nul
echo ============================================
echo FunASR Cross-platform Build - Linux x86_64 + aarch64
echo ============================================

echo [1/4] Setup QEMU multi-arch...
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
if %errorlevel% neq 0 (
    echo QEMU setup failed
    pause
    exit /b 1
)

echo.    Cleanup old builder + switch to desktop-linux...
docker buildx rm funasr-builder 2>nul
docker buildx use desktop-linux 2>nul || docker buildx create --name desktop-linux --driver docker --use

echo [2/4] Build Linux x86_64...
docker buildx build --platform linux/amd64 -t funasr-x86_64 --load .
if %errorlevel% neq 0 (
    echo x86_64 build failed
    pause
    exit /b 1
)

echo [3/4] Build Linux aarch64... 
docker buildx build --platform linux/arm64 -t funasr-aarch64 --load .
if %errorlevel% neq 0 (
    echo aarch64 build failed
    pause
    exit /b 1
)

echo [4/4] Extract binaries...
docker create --name funasr-x86-tmp funasr-x86_64
docker cp funasr-x86-tmp:/build/dist/funasr funasr-linux-x86_64
docker rm funasr-x86-tmp

docker create --name funasr-arm-tmp funasr-aarch64
docker cp funasr-arm-tmp:/build/dist/funasr funasr-linux-aarch64
docker rm funasr-arm-tmp

echo.    Cleanup images...
docker rmi funasr-x86_64 funasr-aarch64 2>nul
docker builder prune -f 2>nul

echo ============================================
echo Build complete!
dir funasr-linux-x86_64 funasr-linux-aarch64
echo ============================================
pause
