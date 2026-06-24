# Runtime hook for PyInstaller - runs before main script
import os
import sys
import site
import pathlib

print(f"[DEBUG] Runtime hook started", flush=True)
print(f"[DEBUG] frozen={getattr(sys, 'frozen', False)}", flush=True)
print(f"[DEBUG] sys.platform={sys.platform}", flush=True)

if getattr(sys, 'frozen', False):
    base_dir = pathlib.Path(sys._MEIPASS)
    print(f"[DEBUG] MEIPASS={sys._MEIPASS}", flush=True)

    if sys.platform == 'win32':
        # Windows: add DLL directories
        for path in [base_dir / 'torch' / 'lib', base_dir / 'paddle' / 'libs']:
            if path.exists():
                try:
                    os.add_dll_directory(str(path))
                    print(f"[DEBUG] Added DLL dir: {path}", flush=True)
                except Exception as e:
                    print(f"[DEBUG] Failed to add DLL dir {path}: {e}", flush=True)

    elif sys.platform.startswith('linux'):
        # Linux: fix site paths and library paths
        print(f"[DEBUG] Original site.getsitepackages={site.getsitepackages()}", flush=True)

        def _fixed_getsitepackages():
            result = site.getsitepackages.__wrapped__() if hasattr(site.getsitepackages, '__wrapped__') else site.getsitepackages()
            print(f"[DEBUG] get_site_packages called, original result={result}", flush=True)
            if result and result[0] is not None:
                print(f"[DEBUG] result is valid, returning as-is", flush=True)
                return result

            # Find site-packages in bundle
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
                    print(f"[DEBUG] Found valid site-packages: {sp}", flush=True)
                    sys.path.insert(0, str(sp))
                    return [str(sp)]

            print(f"[DEBUG] No valid site-packages found", flush=True)
            return result

        site.getsitepackages = _fixed_getsitepackages
        print(f"[DEBUG] site.getsitepackages replaced", flush=True)
        print(f"[DEBUG] Test call: {site.getsitepackages()}", flush=True)

        # Set LD_LIBRARY_PATH
        ld_path = os.environ.get('LD_LIBRARY_PATH', '')
        for lib_dir in [base_dir / 'paddle' / 'libs', base_dir / 'paddlepaddle' / 'libs']:
            if lib_dir.exists():
                os.environ['LD_LIBRARY_PATH'] = f'{lib_dir}:{ld_path}'
                print(f"[DEBUG] Set LD_LIBRARY_PATH to include {lib_dir}", flush=True)
                break
