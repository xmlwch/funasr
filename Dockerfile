FROM python:3.9-slim-buster

WORKDIR /build

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends ffmpeg binutils libgomp1 libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 && rm -rf /var/lib/apt/lists/*

COPY main.py .
COPY requirements.txt .
COPY funasr.spec .

RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt pyinstaller

# ========== DEBUG: 检查 paddlepaddle 和 libpaddle_infer.so ==========
RUN pip show paddlepaddle && \
    echo "=== 查找 libpaddle_infer.so ===" && \
    find /usr -name "libpaddle_infer.so" 2>/dev/null | head -5 && \
    find /root -name "libpaddle_infer.so" 2>/dev/null | head -5 && \
    echo "=== 检查 SO 文件架构和指令集 ===" && \
    SO_PATH=$(find /usr /root -name "libpaddle_infer.so" 2>/dev/null | head -1) && \
    if [ -n "$SO_PATH" ]; then \
        echo "SO_PATH=$SO_PATH" && \
        file "$SO_PATH" && \
        strings "$SO_PATH" | grep -i "avx" | head -10 || echo "未找到AVX字符串"; \
    else \
        echo "未找到 libpaddle_infer.so"; \
    fi && \
    echo "=== 检查 CPU 支持的指令集 ===" && \
    cat /proc/cpuinfo | grep flags | head -1 && \
    echo "=== paddle 路径 ===" && \
    python -c "import paddle; print(paddle.__file__)" && \
    python -c "import paddle.inference as pi; print(pi.__file__)" if available && \
    python -c "from paddle.inference import Config; c = Config(); print(c)" 2>/dev/null || echo "paddle inference import test skipped"
# ========== DEBUG END ==========

RUN pyinstaller funasr.spec
