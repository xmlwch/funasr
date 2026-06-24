# Runtime hook for PyInstaller - adds DLL directories before importing torch
import os
import sys
import pathlib

if sys.platform == 'win32' and getattr(sys, 'frozen', False):
    base_dir = pathlib.Path(sys._MEIPASS)

    # Add torch DLL directory
    torch_dll_path = base_dir / 'torch' / 'lib'
    if torch_dll_path.exists():
        os.add_dll_directory(str(torch_dll_path))

    # Add common DLL paths
    possible_paths = [
        base_dir / 'torch' / 'lib',
        base_dir / 'paddle' / 'libs',
    ]
    for path in possible_paths:
        if path.exists():
            try:
                os.add_dll_directory(str(path))
            except Exception:
                pass
