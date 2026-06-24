# Runtime hook for PyInstaller - runs before main script
import os
import sys
import site
import pathlib

if getattr(sys, 'frozen', False):
    base_dir = pathlib.Path(sys._MEIPASS)
    print(f"[hook] base_dir={base_dir}", flush=True)
    print(f"[hook] original getsitepackages={site.getsitepackages()}", flush=True)

    if sys.platform.startswith('linux'):
        # Find site-packages in bundle (PyInstaller onefile uses pythonX.Y/lib path structure)
        candidates = [
            base_dir / 'python3.9' / 'lib' / 'site-packages',
            base_dir / 'python3.10' / 'lib' / 'site-packages',
            base_dir / 'python3.11' / 'lib' / 'site-packages',
            base_dir / 'python3.12' / 'lib' / 'site-packages',
        ]
        bundled_sp = None
        for sp in candidates:
            if sp.exists():
                bundled_sp = str(sp)
                print(f"[hook] found bundled_sp={bundled_sp}", flush=True)
                break

        if bundled_sp:
            _original_getsitepackages = site.getsitepackages
            def _fixed_getsitepackages():
                result = _original_getsitepackages()
                print(f"[hook] getsitepackages called, result={result}", flush=True)
                if result and None not in result:
                    return result
                return [bundled_sp]
            site.getsitepackages = _fixed_getsitepackages

            if bundled_sp not in sys.path:
                sys.path.insert(0, bundled_sp)

            lib_dir = os.path.dirname(bundled_sp)
            if lib_dir not in sys.path:
                sys.path.insert(0, lib_dir)
            print(f"[hook] added lib_dir={lib_dir} to sys.path", flush=True)

        ld_path = os.environ.get('LD_LIBRARY_PATH', '')
        for sp_candidate in candidates:
            paddle_libs = sp_candidate / 'paddle' / 'libs'
            if paddle_libs.exists():
                os.environ['LD_LIBRARY_PATH'] = f'{paddle_libs}:{ld_path}'
                print(f"[hook] set LD_LIBRARY_PATH={os.environ['LD_LIBRARY_PATH']}", flush=True)
                break

    elif sys.platform == 'win32':
        for path in [base_dir / 'torch' / 'lib', base_dir / 'paddle' / 'libs']:
            if path.exists():
                try:
                    os.add_dll_directory(str(path))
                except Exception:
                    pass
