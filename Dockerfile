FROM python:3.9-slim-buster

WORKDIR /build

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends ffmpeg binutils libgomp1 libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 && rm -rf /var/lib/apt/lists/*

# 设置 pip 镜像源
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip config set install.trusted-host mirrors.aliyun.com

COPY main.py .
COPY requirements.txt .
COPY funasr.spec .

RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt pyinstaller -i https://mirrors.aliyun.com/pypi/simple/

RUN pyinstaller funasr.spec
