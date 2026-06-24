# Runtime hook for PyInstaller - runs before main script
import os
import sys
import site
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
        # Linux: fix site paths and library paths
        _original_getsitepackages = site.getsitepackages
        _site_packages_fixed = [False]

        def _fixed_getsitepackages():
            result = _original_getsitepackages()
            if result and result[0] is not None:
                return result

            if not _site_packages_fixed[0]:
                _site_packages_fixed[0] = True
                # Find site-packages in bundle and add to sys.path
                candidates = [
                    base_dir / 'python39' / 'Lib' / 'site-packages',
                    base_dir / 'python310' / 'Lib' / 'site-packages',
                    base_dir / 'python311' / 'Lib' / 'site-packages',
                    base_dir / 'python312' / 'Lib' / 'site-packages',
                    base_dir / 'python39' / 'lib' / 'site-packages',
                    base_dir / 'python310' / 'lib' / 'site-packages',
                    base_dir / 'python311' / 'lib' / 'site-packages',
                    base_dir / 'python312' / 'lib' / 'site-packages',
                    base_dir / 'Lib' / 'site-packages',
                    base_dir / 'lib' / 'site-packages',
                    base_dir / 'site-packages',
                ]
                for sp in candidates:
                    if sp.exists():
                        sys.path.insert(0, str(sp))
                        break

                # Also try to set paddle lib path
                for lib_dir in [base_dir / 'paddle' / 'libs', base_dir / 'paddlepaddle' / 'libs']:
                    if lib_dir.exists():
                        ld = os.environ.get('LD_LIBRARY_PATH', '')
                        os.environ['LD_LIBRARY_PATH'] = f'{lib_dir}:{ld}'
                        break

            return result

        site.getsitepackages = _fixed_getsitepackages
