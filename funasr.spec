# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import site
import os

# 获取 site-packages 路径（运行时动态获取）
SITE_PACKAGES = site.getsitepackages()[0].replace('\\', '/')

# 获取 SPEC 文件所在目录（用于 runtime hook 路径）
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))

# funasr 包路径
funasr_path = SITE_PACKAGES + '/funasr'

datas = [
    (funasr_path, 'funasr'),
    (SITE_PACKAGES + '/funasr_onnx', 'funasr_onnx'),
    (SITE_PACKAGES + '/Cython', 'Cython'),
]
binaries = [
    (SITE_PACKAGES + '/torch/lib/*.dll', 'torch/lib'),
    (SITE_PACKAGES + '/paddle/libs/*.dll', 'paddle/libs'),
]
hiddenimports = [
    'funasr_onnx',
    'funasr',
    'librosa',
    'soundfile',
    'paddle',
    'paddle.fluid',
    'paddleocr',
    'onnxruntime',
    'numpy',
    'cv2',
    'Cython',
    'Cython.Compiler',
    'Cython.Runtime',
]

# 收集 torch 必要模块（排除有问题的）
tmp_ret = collect_all('torch')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# 收集 torchaudio 必要模块
tmp_ret = collect_all('torchaudio')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# 收集 paddleocr 必要模块
tmp_ret = collect_all('paddleocr')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# 收集 funasr 必要模块
tmp_ret = collect_all('funasr')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# 收集 imageio 必要模块
tmp_ret = collect_all('imageio')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# 收集 imgaug 必要模块
tmp_ret = collect_all('imgaug')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[SITE_PACKAGES],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(SPEC_DIR, 'pyi_rthook.py')],
    excludes=[
        'torch.tests',
        'torch.testing',
        'torch.utils.tensorboard',
        'torch.utils.bottleneck',
        'torch.utils.flopcounter',
        'torch.utils.jit',
        'torch.utils.memory_trace',
        'torch.utils.mobile_optimizer',
        'torch.utils.teardown',
        'paddle.tests',
        'paddleOCR.tests',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='funasr',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
