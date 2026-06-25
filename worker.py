"""
Worker 进程入口模块 - 解决 PyInstaller multiprocessing pickle 问题

使用方式（由 main.py Spawn 调用）:
    python -c "from worker import run_worker; run_worker(...)"
"""
import os
import sys
import time
import queue

# ================= 路径与环境兼容 =================
def get_bundle_dir():
    """获取 PyInstaller 解压后的 bundle 目录（用于加载 DLL/SO）"""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

def get_exe_dir():
    """获取可执行文件所在的目录（用于定位模型）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

BUNDLE_DIR = get_bundle_dir()   # DLL/SO 路径
EXE_DIR = get_exe_dir()        # 模型文件路径

# 设置模型路径环境变量（优先使用用户指定的环境变量，否则使用 exe 同级目录）
if getattr(sys, 'frozen', False):
    if not os.environ.get('FUNASR_MODEL_DIR'):
        os.environ['FUNASR_MODEL_DIR'] = os.path.join(EXE_DIR, "model")
    if not os.environ.get('FUNASR_OCR_MODEL_DIR'):
        os.environ['FUNASR_OCR_MODEL_DIR'] = os.path.join(EXE_DIR, "model", "paddleocr")

# DLL/SO 从 bundle 目录加载
if sys.platform == 'win32':
    for path in [os.path.join(BUNDLE_DIR, 'torch', 'lib'), os.path.join(BUNDLE_DIR, 'Library', 'bin')]:
        if os.path.exists(path):
            try: os.add_dll_directory(path)
            except Exception: pass

if sys.platform.startswith('linux') and getattr(sys, 'frozen', False):
    import site as _site
    import pathlib as _pathlib
    _base = _pathlib.Path(BUNDLE_DIR)
    _site.getsitepackages = lambda: [str(_base)]
    _site.USER_SITE = str(_base)
    for _sub in ['paddle/libs', 'paddle/base']:
        _pp = _base / _sub
        if _pp.exists():
            os.environ['LD_LIBRARY_PATH'] = f'{_pp}:{os.environ.get("LD_LIBRARY_PATH","")}'

# ================= 子进程全局变量 =================
_asr_model = None
_ocr_engine = None

def init_worker_processes():
    global _asr_model, _ocr_engine
    pid = os.getpid()
    print(f"[Worker {pid}] 正在独立加载模型文件 (ASR + OCR)...")

    from funasr_onnx import SenseVoiceSmall
    model_dir = os.environ.get("FUNASR_MODEL_DIR", os.path.join(EXE_DIR, "model"))
    _asr_model = SenseVoiceSmall(model_dir, batch_size=1, quantize=True, intra_op_num_threads=1)

    if sys.platform == 'win32':
        import paddle.inference
        original_switch_ir_optim = paddle.inference.Config.switch_ir_optim
        def fake_switch_ir_optim(self, enable):
            return original_switch_ir_optim(self, False)
        paddle.inference.Config.switch_ir_optim = fake_switch_ir_optim

    from paddleocr import PaddleOCR
    ocr_model_dir = os.environ.get("FUNASR_OCR_MODEL_DIR", os.path.join(EXE_DIR, "model", "paddleocr"))

    is_win = sys.platform == 'win32'
    os.environ['FLAGS_ir_optim'] = '0'

    _ocr_engine = PaddleOCR(
        use_angle_cls=False, lang='ch',
        det_model_dir=os.path.join(ocr_model_dir, "det"),
        rec_model_dir=os.path.join(ocr_model_dir, "rec"),
        show_log=False, use_onnx=False,
        enable_mkldnn=not is_win, cpu_threads=1
    )
    print(f"[Worker {pid}] ✓ 模型加载完成 (OneDNN: {'OFF' if is_win else 'ON'})")

def run_asr_inference(audio_path: str) -> str:
    from funasr.utils.postprocess_utils import rich_transcription_postprocess
    if not os.path.exists(audio_path): raise FileNotFoundError(f"文件不存在: {audio_path}")
    res = _asr_model(audio_path, language="auto", use_itn=True)
    text = res[0] if isinstance(res, list) and res else str(res)
    return rich_transcription_postprocess(text)

def run_ocr_inference(image_path: str) -> str:
    from PIL import Image
    import numpy as np
    if not os.path.exists(image_path): raise FileNotFoundError(f"文件不存在: {image_path}")

    img = Image.open(image_path).convert('RGB')
    w, h = img.size
    if max(w, h) > 2560: img.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
    if min(img.size) < 32:
        scale = 32 / min(img.size)
        img = img.resize((int(img.size[0] * scale), int(img.size[1] * scale)), Image.Resampling.BILINEAR)

    img_rgb = np.ascontiguousarray(np.array(img))
    if img_rgb.shape[0] < 10 or img_rgb.shape[1] < 10: return ""

    result = _ocr_engine.ocr(img_rgb)
    if not result or not result[0]: return ""

    texts = []
    for line in result:
        for item in line:
            if isinstance(item, list) and len(item) == 2:
                text = item[1]
                texts.append(text[0] if isinstance(text, tuple) else text)
    return "\n".join(texts)

def run_worker(task_queue, result_queue, worker_state, pid, idle_timeout):
    """Worker 主循环 - 由 main.py Spawn 调用"""
    # 【修复1】使用自己的真实 PID，不再依赖传入的主进程 PID
    pid = os.getpid()

    # 【修复3】先设置状态，如果初始化失败则标记为 dead
    worker_state[pid] = {'status': 'initializing', 'last_active': time.time()}
    try:
        init_worker_processes()
        worker_state[pid] = {'status': 'idle', 'last_active': time.time()}
    except Exception as e:
        print(f"[Worker {pid}] 模型加载失败: {e}")
        worker_state[pid] = {'status': 'dead', 'last_active': time.time()}
        return

    while True:
        try:
            task = task_queue.get(timeout=5.0)
        except queue.Empty:
            state = worker_state.get(pid)
            if state and state.get('status') == 'idle' and (time.time() - state['last_active'] > idle_timeout):
                alive_count = sum(1 for s in worker_state.values() if s.get('status') not in ('dead', 'initializing'))
                # 始终保留至少 1 个 worker，空闲时只有 > 1 才退出
                if alive_count > 1:
                    print(f"[Worker {pid}] 空闲超时({idle_timeout}s)，主动退出。存活: {alive_count}")
                    worker_state[pid] = {'status': 'dead', 'last_active': time.time()}
                    break
            continue
        except KeyboardInterrupt:
            print(f"[Worker {pid}] 收到 Ctrl+C 信号，正在优雅退出...")
            worker_state[pid] = {'status': 'dead', 'last_active': time.time()}
            break

        if task is None:
            print(f"[Worker {pid}] 收到退出信号 (毒丸)，正在优雅退出...")
            worker_state[pid] = {'status': 'dead', 'last_active': time.time()}
            break

        worker_state[pid] = {'status': 'busy', 'last_active': time.time()}
        try:
            res = run_asr_inference(task['path']) if task['func'] == 'asr' else run_ocr_inference(task['path'])
            result_queue.put((task['id'], res))
        except KeyboardInterrupt:
            print(f"[Worker {pid}] 推理过程中收到 Ctrl+C 信号，中断并退出...")
            worker_state[pid] = {'status': 'dead', 'last_active': time.time()}
            break
        except Exception as e:
            result_queue.put((task['id'], e))

        worker_state[pid] = {'status': 'idle', 'last_active': time.time()}
