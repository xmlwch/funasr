"""共享路径工具 — main.py 与 worker.py 各自需要不同的"基准目录"语义,不要共用。

设计:
  - get_pkg_dir(): main 用,返回 PyInstaller 解压目录(_MEIPASS)
    用途:paddle/libs、paddle/base、bundled bin/ 等随程序一起发布的资源
  - get_exe_dir(): worker / main 的 .env 用,返回 sys.executable 所在目录
    用途:用户放在 exe 旁边的 model/、.env 等运行时配置
"""
import os
import sys


def get_pkg_dir():
    """打包资源目录 — PyInstaller frozen 时是 _MEIPASS 解压目录,否则是源码目录。"""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def get_exe_dir():
    """可执行文件所在目录 — frozen 时是用户运行 exe 的目录,否则是源码目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))
