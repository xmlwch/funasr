FROM python:3.9-slim-buster

WORKDIR /build

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends binutils && \
    rm -rf /var/lib/apt/lists/*

COPY main.py .
COPY requirements.txt .

RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt pyinstaller && \
    pyinstaller --onefile \
      --add-binary "$(python -c 'import onnxruntime; print(onnxruntime.__path__[0])')/capi/*.so*:onnxruntime/capi" \
      --collect-submodules torch \
      --hidden-import=torch \
      --hidden-import=torchaudio \
      --hidden-import=funasr_onnx \
      --hidden-import=funasr \
      --hidden-import=librosa \
      --hidden-import=soundfile \
      --hidden-import=paddle \
      --hidden-import=paddleocr \
      --name funasr \
      main.py
