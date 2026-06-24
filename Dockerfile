FROM python:3.9-slim-buster

WORKDIR /build

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends binutils libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY main.py .
COPY requirements.txt .
COPY funasr.spec .
COPY pyi_rthook.py .

RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt pyinstaller

# Build using spec file
RUN pyinstaller funasr.spec
