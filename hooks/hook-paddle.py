# PyInstaller hook: preimport pygame to fix site.getsitepackages
from PyInstaller.utils.hooks import preimport

# This hook is executed before paddle modules are imported
# It fixes the site.getsitepackages() function to work in PyInstaller bundles

import sys
import os
import site

def _fix_site_packages():
    """Fix site.getsitepackages() for PyInstaller bundles"""
    if not (hasattr(sys, 'frozen') and hasattr(sys, '_MEIPASS')):
        return

    meipass = sys._MEIPASS
    original_getsitepackages = site.getsitepackages

    def fixed_getsitepackages():
        sp = original_getsitepackages()
        if sp and sp[0] is not None:
            return sp

        # Search for site-packages in the bundle
        candidates = [
            os.path.join(meipass, 'python39', 'Lib', 'site-packages'),
            os.path.join(meipass, 'python310', 'Lib', 'site-packages'),
            os.path.join(meipass, 'python311', 'Lib', 'site-packages'),
            os.path.join(meipass, 'python312', 'Lib', 'site-packages'),
            os.path.join(meipass, 'Lib', 'site-packages'),
            os.path.join(meipass, 'site-packages'),
        ]
        for path in candidates:
            if os.path.isdir(path):
                return [path]
        return sp

    site.getsitepackages = fixed_getsitepackages

# Execute immediately
_fix_site_packages()
