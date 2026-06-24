# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import site
import os
import sys
import importlib.util

# 获取 site-packages 路径（运行时动态获取）
def get_site_packages():
    """获取 site-packages 路径，兼容不同环境"""
    # 方法1: 从 site 模块获取
    sp = site.getsitepackages()
    if sp:
        for p in sp:
            if os.path.isdir(p):
                return p.replace('\\', '/')
    return None

def find_package_path(package_name):
    """查找包的实际路径"""
    # 方法1: 通过 importlib.util
    spec = importlib.util.find_spec(package_name)
    if spec and spec.origin:
        return os.path.dirname(spec.origin).replace('\\', '/')
    return None

# 获取 site-packages
SITE_PACKAGES = get_site_packages()

# 如果 site-packages 无效，通过已安装的包路径获取
if not SITE_PACKAGES or not os.path.isdir(SITE_PACKAGES):
    # 尝试从 funasr 包获取路径
    funasr_path = find_package_path('funasr')
    if funasr_path:
        SITE_PACKAGES = os.path.dirname(funasr_path)
    else:
        # 尝试从 torch 获取
        torch_path = find_package_path('torch')
        if torch_path:
            SITE_PACKAGES = os.path.dirname(torch_path)
        else:
            # 最后尝试 sys.path
            for p in sys.path:
                if 'site-packages' in p and os.path.isdir(p):
                    SITE_PACKAGES = p.replace('\\', '/')
                    break

# 获取 SPEC 文件所在目录（用于 runtime hook 路径）
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))

# 查找 funasr 路径
FUNASR_PATH = find_package_path('funasr')
if not FUNASR_PATH:
    FUNASR_PATH = os.path.join(SITE_PACKAGES, 'funasr').replace('\\', '/')

# 查找 funasr_onnx 路径
FUNASR_ONNX_PATH = find_package_path('funasr_onnx')
if not FUNASR_ONNX_PATH:
    FUNASR_ONNX_PATH = os.path.join(SITE_PACKAGES, 'funasr_onnx').replace('\\', '/')

# 查找 Cython 路径
CYTHON_PATH = find_package_path('Cython')
if not CYTHON_PATH:
    CYTHON_PATH = os.path.join(SITE_PACKAGES, 'Cython').replace('\\', '/')

datas = [
    (FUNASR_PATH, 'funasr'),
    (FUNASR_ONNX_PATH, 'funasr_onnx'),
    (CYTHON_PATH, 'Cython'),
]
binaries = []
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

# 收集 torch 必要模块
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
