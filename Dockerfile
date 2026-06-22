FROM python:3.9-slim-buster

WORKDIR /build

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends binutils libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY main.py .
COPY requirements.txt .

RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt pyinstaller

RUN FUNASR_PATH=$(python -c 'import funasr; print(funasr.__path__[0])' 2>/dev/null) && \
    mkdir -p /build/funasr_pkg && \
    cp -r "$FUNASR_PATH" /build/funasr_pkg/funasr && \
    pyinstaller --onefile \
      --add-data /build/funasr_pkg/funasr/version.txt:funasr \
      --collect-submodules torch \
      --hidden-import=torch \
      --hidden-import=torchaudio \
      --hidden-import=funasr_onnx \
      --hidden-import=funasr \
      --hidden-import=librosa \
      --hidden-import=soundfile \
      --hidden-import=paddle \
      --hidden-import=paddle.fluid \
      --hidden-import=paddleocr \
      --collect-all paddle \
      --collect-all paddleocr \
      --collect-all funasr \
      --name funasr \
      main.py
