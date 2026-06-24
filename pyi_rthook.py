# Runtime hook for PyInstaller - runs before main script
import os
import sys
import site
import pathlib

# Debug output to both stdout and stderr
def debug(msg):
    sys.__stdout__.write(f"[hook] {msg}\n")
    sys.__stdout__.flush()

debug(f"hook starting, frozen={getattr(sys, 'frozen', False)}")

if getattr(sys, 'frozen', False):
    base_dir = pathlib.Path(sys._MEIPASS)
    debug(f"base_dir={base_dir}")

    if sys.platform.startswith('linux'):
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
                debug(f"found bundled_sp={bundled_sp}")
                break

        if bundled_sp:
            site.getsitepackages = lambda: [bundled_sp]
            site.USER_SITE = bundled_sp
            debug(f"patched site.getsitepackages and USER_SITE")

            if bundled_sp not in sys.path:
                sys.path.insert(0, bundled_sp)

            lib_dir = os.path.dirname(bundled_sp)
            if lib_dir not in sys.path:
                sys.path.insert(0, lib_dir)
            debug(f"added lib_dir={lib_dir}")

            # Set LD_LIBRARY_PATH
            ld_paths = []
            for subdir in ['paddle/libs', 'paddle/base']:
                p = pathlib.Path(bundled_sp) / subdir
                if p.exists():
                    ld_paths.append(str(p))
                    debug(f"found {p}")
            if ld_paths:
                ld_path = os.environ.get('LD_LIBRARY_PATH', '')
                os.environ['LD_LIBRARY_PATH'] = ':'.join(ld_paths) + f':{ld_path}'
                debug(f"LD_LIBRARY_PATH={os.environ['LD_LIBRARY_PATH']}")

        debug(f"sys.path[:3]={sys.path[:3]}")
        debug(f"site.getsitepackages()={site.getsitepackages()}")
        debug(f"site.USER_SITE={site.USER_SITE}")

    elif sys.platform == 'win32':
        for path in [base_dir / 'torch' / 'lib', base_dir / 'paddle' / 'libs']:
            if path.exists():
                try:
                    os.add_dll_directory(str(path))
                except Exception:
                    pass
