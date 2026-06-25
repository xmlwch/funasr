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

# ========== DEBUG: 检查 paddlepaddle 安装和 SO ==========
RUN set -e && \
    pip show paddlepaddle && \
    echo "=== 检查 SO 文件 ===" && \
    find /usr/local/lib/python3.9 -name "libpaddle_infer.so" 2>/dev/null && \
    python -c "import sys; print('\\n'.join(sys.path))" && \
    python -c "import paddle; print(paddle.__file__)" && \
    echo "paddle import OK"

RUN pyinstaller funasr.spec
