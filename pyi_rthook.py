# Runtime hook for PyInstaller - runs before main script
import os
import sys
import pathlib

if getattr(sys, 'frozen', False):
    base_dir = pathlib.Path(sys._MEIPASS)

    if sys.platform.startswith('linux'):
        # Find the bundled site-packages
        candidates = [
            base_dir / 'lib' / 'python3.9' / 'site-packages',
            base_dir / 'lib' / 'python3.10' / 'site-packages',
            base_dir / 'lib' / 'python3.11' / 'site-packages',
            base_dir / 'lib' / 'python3.12' / 'site-packages',
            base_dir / 'python3.9' / 'lib' / 'site-packages',
            base_dir / 'python3.10' / 'lib' / 'site-packages',
            base_dir / 'python3.11' / 'lib' / 'site-packages',
            base_dir / 'python3.12' / 'lib' / 'site-packages',
        ]
        bundled_sp = None
        for sp in candidates:
            if sp.exists():
                bundled_sp = str(sp)
                if sp not in sys.path:
                    sys.path.insert(0, str(sp))
                break

        # Also add parent lib paths
        if bundled_sp:
            lib_path = os.path.dirname(bundled_sp)
            if lib_path not in sys.path:
                sys.path.insert(0, lib_path)

        # Set LD_LIBRARY_PATH
        ld_path = os.environ.get('LD_LIBRARY_PATH', '')
        for lib_dir in [base_dir / 'paddle' / 'libs', base_dir / 'paddlepaddle' / 'libs']:
            if lib_dir.exists():
                os.environ['LD_LIBRARY_PATH'] = f'{lib_dir}:{ld_path}'
                break

    elif sys.platform == 'win32':
        for path in [base_dir / 'torch' / 'lib', base_dir / 'paddle' / 'libs']:
            if path.exists():
                try:
                    os.add_dll_directory(str(path))
                except Exception:
                    pass
