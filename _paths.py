"""共享路径工具 — main.py 与 worker.py 各自需要不同的"基准目录"语义,不要共用。

设计:
  - get_pkg_dir(): main 用,返回 PyInstaller 解压目录(_MEIPASS)
    用途:paddle/libs、paddle/base、bundled bin/ 等随程序一起发布的资源
  - get_exe_dir(): worker / main 的 .env 用,返回 sys.executable 所在目录
    用途:用户放在 exe 旁边的 model/、.env 等运行时配置
  - setup_bundled_env(): main 和 worker 各自调用,把 _MEIPASS/bin 注入 PATH
    并设 TORCHAUDIO_USE_FFMPEG_PATH(直接告诉 torchaudio,绕开 PATH 检测)
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


def setup_bundled_env():
    """frozen 时把 _MEIPASS/bin 注入 PATH,并设 TORCHAUDIO_USE_FFMPEG_PATH
    等环境变量,让 torchaudio / pydub / ffmpeg-python 等库能找到 ffmpeg。

    main.py 和 worker.py 都需要调用 — worker 是独立 Python 进程,
    不会执行 main.py 的模块级代码,必须自己设。
    """
    if not getattr(sys, 'frozen', False):
        return
    bin_dir = os.path.join(get_pkg_dir(), 'bin')
    if not os.path.isdir(bin_dir):
        return
    # 1) PATH 前置 — 给 subprocess / shutil.which 用
    os.environ['PATH'] = bin_dir + os.pathsep + os.environ.get('PATH', '')
    # 2) 直接告诉 torchaudio 等库 ffmpeg 在哪(绕开 PATH/shutil.which)
    for env_name, fname in (
        ('TORCHAUDIO_USE_FFMPEG_PATH', 'ffmpeg'),  # torchaudio 0.10+
        ('FFMPEG_BINARY', 'ffmpeg'),                # pydub / ffmpeg-python
    ):
        ffmpeg_path = os.path.join(bin_dir, fname)
        if os.path.isfile(ffmpeg_path):
            os.environ[env_name] = ffmpeg_path
