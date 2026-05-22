#!/bin/bash
# 跨平台打包脚本 - 在任意 Linux x86_64 机器上运行

# 1. 启用 QEMU 多架构支持
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

# 2. 创建 buildx builder
docker buildx create --name funasr-builder --use 2>/dev/null || docker buildx use funasr-builder
docker buildx inspect --bootstrap

# 3. 打包 Linux aarch64
docker buildx build --platform linux/arm64 \
  -t funasr-aarch64 \
  --load \
  .

# 4. 打包 Linux x86_64
docker buildx build --platform linux/amd64 \
  -t funasr-x86_64 \
  --load \
  .

# 5. 提取二进制
docker run --rm funasr-x86_64 cat /main > main-linux-x86_64
docker run --rm funasr-aarch64 cat /main > main-linux-aarch64
chmod +x main-linux-x86_64 main-linux-aarch64

echo "打包完成:"
ls -lh main-linux-x86_64 main-linux-aarch64
