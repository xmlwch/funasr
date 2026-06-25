FROM python:3.9-slim-buster

WORKDIR /build

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends ffmpeg binutils libgomp1 libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 && rm -rf /var/lib/apt/lists/*

COPY main.py .
COPY requirements.txt .
COPY funasr.spec .

# 使用 2.5.2 替代 2.6.2，旧版本对 CPU 指令集要求可能更宽松
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir paddlepaddle==2.5.2 && \
    pip install --no-cache-dir pyinstaller more_itertools && \
    pip install --no-cache-dir funasr-onnx funasr onnxruntime paddleocr==2.9.1

RUN pyinstaller funasr.spec
