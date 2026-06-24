# Preload script for PyInstaller - must be imported before any other imports
import sys
import os
import site

# Only run in PyInstaller bundle
if hasattr(sys, 'frozen') and hasattr(sys, '_MEIPASS'):
    meipass = sys._MEIPASS

    _original_getsitepackages = site.getsitepackages

    def _fixed_getsitepackages():
        result = _original_getsitepackages()
        # Check if result is valid (not [None])
        if result and result[0] is not None:
            return result

        # Search for site-packages in the bundle
        candidates = [
            os.path.join(meipass, 'python39', 'Lib', 'site-packages'),
            os.path.join(meipass, 'python310', 'Lib', 'site-packages'),
            os.path.join(meipass, 'python311', 'Lib', 'site-packages'),
            os.path.join(meipass, 'python312', 'Lib', 'site-packages'),
            os.path.join(meipass, 'python39', 'lib', 'site-packages'),
            os.path.join(meipass, 'python310', 'lib', 'site-packages'),
            os.path.join(meipass, 'python311', 'lib', 'site-packages'),
            os.path.join(meipass, 'python312', 'lib', 'site-packages'),
            os.path.join(meipass, 'Lib', 'site-packages'),
            os.path.join(meipass, 'lib', 'site-packages'),
            os.path.join(meipass, 'site-packages'),
        ]
        for path in candidates:
            if os.path.isdir(path):
                return [path]
        return result

    site.getsitepackages = _fixed_getsitepackages
