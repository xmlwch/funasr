FROM python:3.10-slim-buster

WORKDIR /build

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends binutils libgomp1 libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 ffmpeg ccache && rm -rf /var/lib/apt/lists/*

# 把 ffmpeg / ccache 单独收集到 /build/bin/，PyInstaller spec 会引用它们，
# 运行时 main.py 会把 _MEIPASS/bin 追加到 PATH，消除 torchaudio 与 PaddlePaddle 的告警。
RUN mkdir -p /build/bin && \
    cp -L /usr/bin/ffmpeg /usr/bin/ffprobe /usr/bin/ccache /build/bin/ 2>/dev/null || true && \
    chmod +x /build/bin/* 2>/dev/null || true && \
    ls -la /build/bin/

# torchaudio 2.x 不再调用 ffmpeg 命令行,而是 dlopen libav*.so/libsw*.so/libpostproc.so
# 这些动态库 — 复制到 /build/lib/ 让 PyInstaller 打包,运行时 main/worker 注入 LD_LIBRARY_PATH
#
# 不硬编码 /usr/lib/x86_64-linux-gnu/,改用 find 自动定位:
#   - 跨 Debian 版本(虽然路径都叫 *-linux-gnu,但万一改了)
#   - 跨架构(x86_64 / aarch64 / 等)
# 同时先打印找到的源文件清单,拷完再 ls 验证结果
RUN mkdir -p /build/lib && \
    echo "=== 源文件(finder): ===" && \
    find /usr/lib -maxdepth 3 \( -name 'libav*.so*' -o -name 'libsw*.so*' -o -name 'libpostproc.so*' \) 2>/dev/null && \
    find /usr/lib -maxdepth 3 \( -name 'libav*.so*' -o -name 'libsw*.so*' -o -name 'libpostproc.so*' \) -exec cp -L {} /build/lib/ \; 2>/dev/null || true && \
    chmod +x /build/lib/* 2>/dev/null || true && \
    echo "=== 拷完后 /build/lib/: ===" && \
    ls -la /build/lib/

COPY main.py .
COPY worker.py .
COPY _paths.py .
COPY requirements.txt .
COPY funasr.spec .

RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir paddlepaddle==3.3.1 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/ && \
    pip install --no-cache-dir -r requirements.txt pyinstaller

RUN pyinstaller funasr.spec
