# Runtime hook for PyInstaller - runs before main script
import os
import sys
import site
import pathlib

if getattr(sys, 'frozen', False):
    base_dir = pathlib.Path(sys._MEIPASS)

    if sys.platform.startswith('linux'):
        # PyInstaller onefile on Linux uses: $MEIPASS/lib/pythonX.Y/site-packages
        candidates = [
            base_dir / 'lib' / 'python3.9' / 'site-packages',
            base_dir / 'lib' / 'python3.10' / 'site-packages',
            base_dir / 'lib' / 'python3.11' / 'site-packages',
            base_dir / 'lib' / 'python3.12' / 'site-packages',
        ]
        bundled_sp = None
        for sp in candidates:
            if sp.exists():
                bundled_sp = str(sp)
                break

        if bundled_sp:
            # Fix site.getsitepackages - original may return wrong path in PyInstaller env
            _original_getsitepackages = site.getsitepackages
            def _fixed_getsitepackages():
                return [bundled_sp]
            site.getsitepackages = _fixed_getsitepackages

            # Fix site.USER_SITE - must not be None
            if site.USER_SITE is None:
                site.USER_SITE = bundled_sp

            # Add to sys.path
            if bundled_sp not in sys.path:
                sys.path.insert(0, bundled_sp)

            lib_dir = os.path.dirname(bundled_sp)  # .../lib
            if lib_dir not in sys.path:
                sys.path.insert(0, lib_dir)

            # Set LD_LIBRARY_PATH for paddle libs
            paddle_libs = pathlib.Path(bundled_sp) / 'paddle' / 'libs'
            if paddle_libs.exists():
                ld_path = os.environ.get('LD_LIBRARY_PATH', '')
                os.environ['LD_LIBRARY_PATH'] = f'{paddle_libs}:{ld_path}'

    elif sys.platform == 'win32':
        for path in [base_dir / 'torch' / 'lib', base_dir / 'paddle' / 'libs']:
            if path.exists():
                try:
                    os.add_dll_directory(str(path))
                except Exception:
                    pass
