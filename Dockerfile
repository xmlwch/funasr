FROM python:3.10-slim-bookworm

WORKDIR /build

# 【生产改造 C2】bookworm(原 buster 已 EOL 2024-06)
# libgl1-mesa-glx 在 bookworm 已移除,改用 libgl1 + libglx-mesa0
RUN apt-get update && apt-get install -y --no-install-recommends \
    binutils libgomp1 libgl1 libglx-mesa0 libglib2.0-0 \
    libsm6 libxext6 ffmpeg ccache \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 把 ffmpeg / ccache 单独收集到 /build/bin/，PyInstaller spec 会引用它们，
# 运行时 main.py 会把 _MEIPASS/bin 追加到 PATH，消除 torchaudio 与 PaddlePaddle 的告警。
RUN mkdir -p /build/bin && \
    cp -L /usr/bin/ccache /build/bin/ 2>/dev/null || true && \
    chmod +x /build/bin/* 2>/dev/null || true && \
    ls -la /build/bin/


COPY main.py .
COPY worker.py .
COPY _paths.py .
COPY requirements.txt .
COPY funasr.spec .

RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir paddlepaddle==3.3.1 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/ && \
    pip install --no-cache-dir -r requirements.txt pyinstaller

RUN pyinstaller funasr.spec
