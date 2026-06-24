# Runtime hook for PyInstaller - runs before main script
import os
import sys
import pathlib

if getattr(sys, 'frozen', False):
    base_dir = pathlib.Path(sys._MEIPASS)

    if sys.platform == 'win32':
        # Windows: add DLL directories for torch and paddle
        for path in [base_dir / 'torch' / 'lib', base_dir / 'paddle' / 'libs']:
            if path.exists():
                try:
                    os.add_dll_directory(str(path))
                except Exception:
                    pass

    elif sys.platform.startswith('linux'):
        # Linux: fix site.getsitepackages() and set LD_LIBRARY_PATH
        import site

        original_getsitepackages = site.getsitepackages
        _original_result = [None]  # closure to store original result

        def _fixed_getsitepackages():
            """Fixed get_site_packages that searches in the bundle"""
            result = original_getsitepackages()
            if result and result[0] is not None:
                return result

            # Search for site-packages in the bundle
            candidates = [
                str(base_dir / 'python39' / 'Lib' / 'site-packages'),
                str(base_dir / 'python310' / 'Lib' / 'site-packages'),
                str(base_dir / 'python311' / 'Lib' / 'site-packages'),
                str(base_dir / 'python312' / 'Lib' / 'site-packages'),
                str(base_dir / 'Lib' / 'site-packages'),
                str(base_dir / 'site-packages'),
            ]
            for path in candidates:
                if os.path.isdir(path):
                    return [path]
            return result

        site.getsitepackages = _fixed_getsitepackages

        # Set LD_LIBRARY_PATH for libpaddle.so
        ld_path = os.environ.get('LD_LIBRARY_PATH', '')
        new_paths = []

        paddle_libs = base_dir / 'paddle' / 'libs'
        if paddle_libs.exists():
            new_paths.append(str(paddle_libs))

        torch_lib = base_dir / 'torch' / 'lib'
        if torch_lib.exists():
            new_paths.append(str(torch_lib))

        if new_paths:
            os.environ['LD_LIBRARY_PATH'] = ':'.join(new_paths) + ':' + ld_path
