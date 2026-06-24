# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import site
import os
import sys
import glob
import importlib.util

# 获取 site-packages 路径（运行时动态获取）
def get_site_packages():
    """获取 site-packages 路径，兼容不同环境"""
    sp = site.getsitepackages()
    if sp:
        for p in sp:
            if os.path.isdir(p):
                return p.replace('\\', '/')
    return None

def find_package_path(package_name):
    """查找包的实际路径"""
    spec = importlib.util.find_spec(package_name)
    if spec and spec.origin:
        return os.path.dirname(spec.origin).replace('\\', '/')
    return None

# 获取 site-packages
SITE_PACKAGES = get_site_packages()

if not SITE_PACKAGES or not os.path.isdir(SITE_PACKAGES):
    funasr_path = find_package_path('funasr')
    if funasr_path:
        SITE_PACKAGES = os.path.dirname(funasr_path)
    else:
        torch_path = find_package_path('torch')
        if torch_path:
            SITE_PACKAGES = os.path.dirname(torch_path)
        else:
            for p in sys.path:
                if 'site-packages' in p and os.path.isdir(p):
                    SITE_PACKAGES = p.replace('\\', '/')
                    break

# 获取 SPEC 文件所在目录
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))

# 查找各包路径
FUNASR_PATH = find_package_path('funasr') or os.path.join(SITE_PACKAGES, 'funasr').replace('\\', '/')
FUNASR_ONNX_PATH = find_package_path('funasr_onnx') or os.path.join(SITE_PACKAGES, 'funasr_onnx').replace('\\', '/')
CYTHON_PATH = find_package_path('Cython') or os.path.join(SITE_PACKAGES, 'Cython').replace('\\', '/')

# 查找 paddle libs 路径
def find_paddle_libs():
    paddle_path = find_package_path('paddle')
    if paddle_path:
        libs_path = os.path.join(paddle_path, 'libs').replace('\\', '/')
        if os.path.isdir(libs_path):
            return libs_path
    libs_path = os.path.join(SITE_PACKAGES, 'paddle', 'libs').replace('\\', '/')
    if os.path.isdir(libs_path):
        return libs_path
    return None

PADDLE_LIBS_PATH = find_paddle_libs()

datas = [
    (FUNASR_PATH, 'funasr'),
    (FUNASR_ONNX_PATH, 'funasr_onnx'),
    (CYTHON_PATH, 'Cython'),
]

# paddle libs - 根据平台选择文件类型
binaries = []
if PADDLE_LIBS_PATH:
    import sys
    if sys.platform == 'win32':
        binaries.append((os.path.join(PADDLE_LIBS_PATH, '*.dll').replace('\\', '/'), 'paddle/libs'))
    else:
        binaries.append((os.path.join(PADDLE_LIBS_PATH, '*.so').replace('\\', '/'), 'paddle/libs'))

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

# 收集各模块
for mod in ['torch', 'torchaudio', 'paddleocr', 'funasr', 'imageio', 'imgaug']:
    tmp_ret = collect_all(mod)
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
