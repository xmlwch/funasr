FROM python:3.9-slim-buster

WORKDIR /build

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends ffmpeg binutils libgomp1 libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 gdb libc6-dbg && rm -rf /var/lib/apt/lists/*

COPY main.py .
COPY requirements.txt .
COPY funasr.spec .

RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt pyinstaller

# ========== DEBUG: 检查 paddlepaddle 和崩溃原因 ==========
RUN pip show paddlepaddle && \
    echo "=== 检查 CPU 信息 ===" && \
    lscpu && \
    echo "=== 检查 glibc 版本 ===" && \
    ldd --version && \
    echo "=== 尝试 import paddle 获取 backtrace ===" && \
    python -c "import paddle; print('paddle OK')" 2>&1 || \
    (echo "=== 使用 gdb 抓取 backtrace ===" && \
     gdb -batch -ex "run" -ex "bt" --args python -c "import paddle" 2>&1 | tail -50)

RUN pyinstaller funasr.spec
