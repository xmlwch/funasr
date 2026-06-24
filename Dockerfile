FROM python:3.9-slim-buster

WORKDIR /build

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends binutils libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY main.py .
COPY requirements.txt .
COPY hooks/ /build/hooks/
COPY pyi_rthook.py /build/

RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt pyinstaller

# Get site-packages path dynamically and copy packages
RUN FUNASR_SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])") && \
    mkdir -p /build/funasr_pkg && \
    cp -r $FUNASR_SITE_PACKAGES/funasr /build/funasr_pkg/ && \
    cp -r $FUNASR_SITE_PACKAGES/funasr_onnx /build/funasr_pkg/ && \
    cp -r $FUNASR_SITE_PACKAGES/Cython /build/funasr_pkg/ && \
    cp -r $FUNASR_SITE_PACKAGES/paddle /build/funasr_pkg/ && \
    cp -r $FUNASR_SITE_PACKAGES/paddlepaddle /build/funasr_pkg/ 2>/dev/null || true && \
    pyinstaller --onefile \
      --additional-hooks-dir /build/hooks \
      --runtime-hook /build/pyi_rthook.py \
      --add-data /build/funasr_pkg/funasr:funasr \
      --add-data /build/funasr_pkg/funasr_onnx:funasr_onnx \
      --add-data /build/funasr_pkg/Cython:Cython \
      --add-data /build/funasr_pkg/paddle:paddle \
      --collect-all torch \
      --collect-all torchaudio \
      --collect-all paddle \
      --collect-all paddleocr \
      --collect-all funasr \
      --collect-all imageio \
      --collect-all imgaug \
      --hidden-import=funasr_onnx \
      --hidden-import=funasr \
      --hidden-import=librosa \
      --hidden-import=soundfile \
      --hidden-import=paddle \
      --hidden-import=paddle.fluid \
      --hidden-import=paddleocr \
      --hidden-import=onnxruntime \
      --hidden-import=numpy \
      --hidden-import=cv2 \
      --hidden-import=Cython \
      --hidden-import=Cython.Compiler \
      --hidden-import=Cython.Runtime \
      --exclude-module=torch.tests \
      --exclude-module=torch.testing \
      --exclude-module=torch.utils.tensorboard \
      --exclude-module=paddle.tests \
      --exclude-module=paddleOCR.tests \
      --name funasr \
      main.py
