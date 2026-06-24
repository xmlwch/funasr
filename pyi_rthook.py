# Runtime hook for PyInstaller - runs before main script
import os
import sys
import site
import pathlib

if getattr(sys, 'frozen', False):
    base_dir = pathlib.Path(sys._MEIPASS)

    if sys.platform.startswith('linux'):
        # Find site-packages in bundle
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
            # Override site.getsitepackages to return the correct path
            _original_getsitepackages = site.getsitepackages
            def _fixed_getsitepackages():
                result = _original_getsitepackages()
                if result and None not in result:
                    return result
                return [bundled_sp]
            site.getsitepackages = _fixed_getsitepackages

            # Add to sys.path if not there
            if bundled_sp not in sys.path:
                sys.path.insert(0, bundled_sp)

            # Add parent lib directory too
            lib_dir = os.path.dirname(bundled_sp)
            if lib_dir not in sys.path:
                sys.path.insert(0, lib_dir)

        # Set LD_LIBRARY_PATH for paddle libs
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
