FROM python:3.10-slim-buster

WORKDIR /build

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends binutils libgomp1 libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 && rm -rf /var/lib/apt/lists/*

COPY main.py .
COPY worker.py .
COPY requirements.txt .
COPY funasr.spec .

RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir paddlepaddle==3.3.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/ && \
    pip install --no-cache-dir -r requirements.txt pyinstaller

RUN pyinstaller funasr.spec
