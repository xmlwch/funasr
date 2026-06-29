# ====================================================================
# FunASR build container — 仅用于 pyinstaller 打包 build-only
# 产物是 self-contained 的二进制(funasr-linux-{x86_64,aarch64}),
# 不用于运行服务。最终镜像用户从 GitHub Release 下载并自部署。
#
# 历史变更:
#   - v0.9.2 之前:slim-buster(libgl1-mesa-glx) — buster 2024-06 EOL
#   - v0.9.2:    slim-bookworm + libgl1 + libglx-mesa0 + apt-get clean
# ====================================================================

FROM python:3.10-slim-bookworm

# OCI labels(registry / image scanner 友好)
LABEL org.opencontainers.image.title="funasr-builder" \
      org.opencontainers.image.description="PyInstaller build env for FunASR + PaddleOCR service" \
      org.opencontainers.image.source="https://github.com/xmlwch/funasr" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="xmlwch"

WORKDIR /build

# ----------------------------------------------------------------
# 系统依赖 — FunASR / PaddleOCR / torchaudio 需要的 .so
#   binutils:          strip / ld
#   libgomp1:          OpenMP(torch 多线程)
#   libgl1 + libglx-mesa0: bookworm 拆分了 mesa-glx,必须两个都装
#   libglib2.0-0:      paddle 间接依赖
#   libsm6 / libxext6: cv2 依赖
#   ffmpeg:            torchaudio 后端
#   ccache:            paddle 编译期调用,无 .so 但 spec 仍打包它消除告警
# ----------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        binutils \
        libgomp1 \
        libgl1 libglx-mesa0 \
        libglib2.0-0 \
        libsm6 libxext6 \
        ffmpeg \
        ccache \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# ccache 提示"找不到"会触发 paddle 告警 — 收集到 /build/bin 让 spec 打包
RUN mkdir -p /build/bin && \
    cp -L /usr/bin/ccache /build/bin/ 2>/dev/null || true && \
    (chmod +x /build/bin/* 2>/dev/null || true) && \
    ls -la /build/bin/ || true

# 把源码 + 依赖清单先 COPY — 依赖装好后改源码会复用 pip 缓存层
# 注意:L1 拆分后,main.py 依赖 pool.py / handler.py / security.py,缺一不可
COPY requirements.txt ./
COPY main.py worker.py _paths.py ./
COPY pool.py security.py handler.py ./
COPY funasr.spec ./

# ----------------------------------------------------------------
# Python 依赖:
#   1) torch CPU 版:约 200MB,装自 PyTorch 官方源(快)
#   2) paddlepaddle CPU:官方源固定 3.3.1
#   3) 项目运行时依赖:requirements.txt (funasr / paddleocr / ...)
#   4) pyinstaller:仅打包时需要
# 把 pyinstaller 单独 RUN,源码改动不重装 torch/paddle(利用层缓存)
# ----------------------------------------------------------------
RUN pip install --no-cache-dir \
        torch torchaudio \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir \
        paddlepaddle==3.3.1 \
        -i https://www.paddlepaddle.org.cn/packages/stable/cpu/ \
    && pip install --no-cache-dir \
        -r requirements.txt

RUN pip install --no-cache-dir pyinstaller

# 打包 — 产物在 /build/dist/funasr,CI 步骤 docker cp 出来
RUN pyinstaller funasr.spec --clean --noconfirm
