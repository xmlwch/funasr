# Runtime hook for PyInstaller - runs before main script
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
        # Linux: set LD_LIBRARY_PATH for paddle libs
        ld_path = os.environ.get('LD_LIBRARY_PATH', '')
        new_paths = []

        for lib_dir in [base_dir / 'paddle' / 'libs', base_dir / 'paddlepaddle' / 'libs']:
            if lib_dir.exists():
                new_paths.append(str(lib_dir))

        if new_paths:
            os.environ['LD_LIBRARY_PATH'] = ':'.join(new_paths) + ':' + ld_path
