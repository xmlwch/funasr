# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_data_files
import os
import sys

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

# Cython 的 Utility 文件（包含 CppSupport.cpp 等）需要单独收集
try:
    cython_datas = collect_data_files('Cython')
    datas += cython_datas
    print(f"Collected Cython data files: {len(cython_datas)} entries")
except Exception as e:
    print(f"Warning: Failed to collect Cython data files: {e}")

# 如果 collect_data_files 没有包含 Utility 目录，直接添加
if not any('Cython' in str(d[0]) and 'Utility' in str(d[0]) for d in datas):
    import Cython
    cython_path = os.path.dirname(Cython.__file__)
    utility_src = os.path.join(cython_path, 'Utility')
    if os.path.exists(utility_src):
        datas.append((utility_src, 'Cython/Utility'))
        print(f"Added Cython/Utility from: {utility_src}")

# 补充一些常见的隐藏导入，防止运行时 ModuleNotFoundError
hiddenimports += [
    'librosa', 'soundfile', 'numpy', 'cv2', 'Cython',
    'Cython.Compiler', 'Cython.Runtime',
    'paddle.fluid', 'paddle.nn', 'paddle.tensor',
    'paddle.optimizer', 'more_itertools',
    
    # 【关键新增】：强制打包我们拆分出来的 worker 模块！
    # 这是解决 PyInstaller 多进程 AttributeError 的核心
    'worker'
]

a = Analysis(
    ['main.py'],
    pathex=[SPEC_DIR],  # 确保 PyInstaller 能在 SPEC 同级目录找到 worker.py
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    
    # 【关键修改】：清空自定义的 runtime_hooks。
    # 采用拆分 worker.py 的标准方案后，PyInstaller 内置的多进程支持已足够，
    # 移除自定义 hook 可避免潜在的冲突和找不到文件的报错。
    runtime_hooks=[], 
    
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