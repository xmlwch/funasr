# Paddle preload fix for PyInstaller
# This must be imported BEFORE paddle
import sys
import os
import site

if hasattr(sys, 'frozen') and hasattr(sys, '_MEIPASS'):
    # We're in a PyInstaller bundle
    meipass = sys._MEIPASS

    # Fix site.getsitepackages() to return the bundled site-packages
    def fixed_getsitepackages():
        sp = []
        # Check common locations in the bundle
        for subdir in ['python39.zip', 'python310.zip', 'python311.zip', 'python312.zip']:
            path = os.path.join(meipass, subdir.replace('.zip', ''), 'Lib', 'site-packages')
            if os.path.isdir(path):
                sp.append(path)
                break
        # Try direct site-packages
        if not sp:
            path = os.path.join(meipass, 'site-packages')
            if os.path.isdir(path):
                sp.append(path)
        # Try Lib/site-packages
        if not sp:
            path = os.path.join(meipass, 'Lib', 'site-packages')
            if os.path.isdir(path):
                sp.append(path)
        return sp if sp else [os.path.dirname(os.path.dirname(meipass))]

    # Override site.getsitepackages
    site.getsitepackages = fixed_getsitepackages
