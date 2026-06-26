"""共享路径工具 — main.py 与 worker.py 都会用到,放在独立模块避免循环依赖。"""
import os
import sys


def get_base_dir():
    """frozen 时取 sys.executable 所在目录,否则取 __file__ 所在目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))
