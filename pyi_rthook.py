# Runtime hook for PyInstaller - adds library directories before importing
import os
import sys
import pathlib

if getattr(sys, 'frozen', False):
    base_dir = pathlib.Path(sys._MEIPASS)

    if sys.platform == 'win32':
        # Windows: add DLL directories
        for path in [base_dir / 'torch' / 'lib', base_dir / 'paddle' / 'libs']:
            if path.exists():
                try:
                    os.add_dll_directory(str(path))
                except Exception:
                    pass
    elif sys.platform.startswith('linux'):
        # Linux: add library path for libpaddle.so
        ld_path = os.environ.get('LD_LIBRARY_PATH', '')
        new_paths = []

        # Add paddle libs path
        paddle_libs = base_dir / 'paddle' / 'libs'
        if paddle_libs.exists():
            new_paths.append(str(paddle_libs))

        # Add torch lib path if exists
        torch_lib = base_dir / 'torch' / 'lib'
        if torch_lib.exists():
            new_paths.append(str(torch_lib))

        if new_paths:
            os.environ['LD_LIBRARY_PATH'] = ':'.join(new_paths) + ':' + ld_path
