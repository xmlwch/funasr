# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import os

# 获取 SPEC 文件所在目录
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))

datas = []
binaries = []
hiddenimports = []

# 需要收集所有资源（数据、动态库、隐藏导入）的核心包
packages_to_collect = [
    'torch', 'torchaudio', 
    'paddle', 'paddleocr', 
    'funasr', 'funasr_onnx', 
    'onnxruntime',
    'imageio', 'imgaug'
]

for mod in packages_to_collect:
    try:
        print(f"Collecting all resources for: {mod}")
        tmp_ret = collect_all(mod)
        datas += tmp_ret[0]
        binaries += tmp_ret[1]
        hiddenimports += tmp_ret[2]
    except Exception as e:
        print(f"Warning: Failed to collect {mod}: {e}")

# 补充一些常见的隐藏导入，防止运行时 ModuleNotFoundError
hiddenimports += [
    'librosa', 'soundfile', 'numpy', 'cv2', 'Cython',
    'paddle.fluid', 'paddle.nn', 'paddle.tensor',
    'paddle.optimizer', 'more_itertools'
]

a = Analysis(
    ['main.py'],
    pathex=[],  # <--- 【关键修复】：必须为空！让 PyInstaller 自动处理 sys.path
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(SPEC_DIR, 'pyi_rthook.py')], # 确保 pyi_rthook.py 存在
    excludes=[
        'torch.tests', 'torch.testing', 'torch.utils.tensorboard',
        'torch.utils.bottleneck', 'torch.utils.flopcounter',
        'paddle.tests', 'paddleOCR.tests'
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
    upx=False,      # ARM64 下建议关闭 upx，容易出问题
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)