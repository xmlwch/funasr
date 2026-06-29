import os
import sys
import time
import queue
import logging

# worker 用 get_exe_dir:模型文件是用户放在 exe 旁边的,不在 _MEIPASS
# setup_bundled_env 把 _MEIPASS/bin 注入 PATH,让 torchaudio 在 worker 里
# 也能找到 ffmpeg(worker 是独立进程,不会跑 main.py 的模块级代码)
from _paths import get_exe_dir, setup_bundled_env

BASE_DIR = get_exe_dir()
setup_bundled_env()

# 【生产改造 M1】worker 进程独立 logger,以 PID 区分多 worker 输出
worker_logger = logging.getLogger(f'funasr.worker.{os.getpid()}')

# 【生产改造 M2】worker 队列 get 超时(秒),保持与 main.py 同步调优
WORKER_QUEUE_GET_TIMEOUT = 5.0

# 【生产改造 M6】PaddlePaddle Windows 上的 IR 优化会让 PaddleOCR 启动崩溃
# 提到模块顶层,每个 worker 进程 import 时只 patch 一次(原在 init_worker_processes 内每次加载都重写)
# 关键:main 进程的 patch 不会跨 spawn 边界继承给 worker,所以必须在 worker.py 顶层每个进程都做
if sys.platform == 'win32':
    try:
        import paddle.inference
        _orig_switch_ir_optim = paddle.inference.Config.switch_ir_optim
        paddle.inference.Config.switch_ir_optim = lambda self, enable: _orig_switch_ir_optim(self, False)
    except (ImportError, AttributeError):
        pass  # paddle 未安装或版本变化,无影响


# ================= 子进程全局变量 =================
_asr_model = None
_ocr_engine = None

def init_worker_processes(model_type: str):
    """进程池初始化：每个 Worker 按 model_type 加载对应模型

    model_type: 'asr' 或 'ocr'
    """
    global _asr_model, _ocr_engine
    pid = os.getpid()
    model_type = model_type.lower()
    worker_logger.info("[Worker %d] 正在加载 %s 模型...", pid, model_type.upper())

    if model_type == 'asr':
        from funasr_onnx import SenseVoiceSmall
        model_dir = os.environ.get("FUNASR_MODEL_DIR", os.path.join(BASE_DIR, "model"))
        _asr_model = SenseVoiceSmall(model_dir, batch_size=1, quantize=True, intra_op_num_threads=1)
        worker_logger.info("[Worker %d] ✓ ASR 模型加载完成", pid)
    elif model_type == 'ocr':
        # 【生产改造 M6】PaddlePaddle Windows IR 优化 monkey patch 已移至 worker.py 顶层
        # 此处不再重复 patch

        from paddleocr import PaddleOCR
        ocr_model_dir = os.environ.get("FUNASR_OCR_MODEL_DIR", os.path.join(BASE_DIR, "model", "paddleocr"))
        is_win = sys.platform == 'win32'
        os.environ['FLAGS_ir_optim'] = '0'
        _ocr_engine = PaddleOCR(
            use_angle_cls=True, lang='ch',
            det_model_dir=os.path.join(ocr_model_dir, "det"),
            rec_model_dir=os.path.join(ocr_model_dir, "rec"),
            cls_model_dir=os.path.join(ocr_model_dir, "cls"),
            show_log=False, use_onnx=False,
            enable_mkldnn=not is_win, cpu_threads=1
        )
        worker_logger.info("[Worker %d] ✓ OCR 模型加载完成 (OneDNN: %s)",
                           pid, 'OFF' if is_win else 'ON')
    else:
        raise ValueError(f"未知的 model_type: {model_type!r}（应为 'asr' 或 'ocr'）")

# ================= 推理函数 =================
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
            if isinstance(item, list) and len(item) >= 2:
                text_data = item[1]
                if isinstance(text_data, tuple):
                    texts.append(text_data[0])
                else:
                    texts.append(str(text_data))
    return "\n".join(texts)

# ================= 弹性 Worker 循环 =================
def elastic_worker_loop(task_queue, results, worker_state, pid_placeholder, idle_timeout, model_type, min_workers=1):
    # 【关键修复 1】：在子进程内部获取真实的 PID，覆盖掉主进程传来的占位符 0
    real_pid = os.getpid()

    # 【关键修复 2】：增加 try/except，防止模型加载失败时静默崩溃
    try:
        init_worker_processes(model_type)
        worker_state[real_pid] = {'status': 'idle', 'last_active': time.time()}
    except Exception as e:
        worker_logger.error("[Worker %d] 模型加载失败: %s", real_pid, e)
        worker_state[real_pid] = {'status': 'dead', 'last_active': time.time()}
        return  # 直接退出，让主进程的监控线程去清理

    while True:
        try:
            task = task_queue.get(timeout=WORKER_QUEUE_GET_TIMEOUT)
        except queue.Empty:
            state = worker_state.get(real_pid)
            if state and (time.time() - state['last_active'] > idle_timeout):
                alive_count = sum(1 for s in worker_state.values() if s.get('status') != 'dead')
                # 【生产改造】至少保留 min_workers 个 alive(主进程通过 args 传入)
                # 旧逻辑硬编码 > 1 会让池子空闲后缩到 1,生产突发流量要冷启动
                if alive_count > min_workers:
                    # 【注意】"至少保留 1 个 alive"是 best-effort:两个 worker 几乎同时
                    # 走到这里时都可能看到 alive_count>1 而双双退出。系统会自愈
                    # (下一个 submit 看到 alive_pids 为空,触发 start_worker)。
                    worker_logger.info("[Worker %d] 空闲超时,主动退出以释放资源。", real_pid)
                    worker_state[real_pid] = {'status': 'dead', 'last_active': time.time()}
                    break
            continue
        except KeyboardInterrupt:
            worker_logger.info("[Worker %d] 收到 Ctrl+C 信号,正在优雅退出...", real_pid)
            worker_state[real_pid] = {'status': 'dead', 'last_active': time.time()}
            break

        if task is None:
            worker_logger.info("[Worker %d] 收到退出信号 (毒丸),正在优雅退出...", real_pid)
            worker_state[real_pid] = {'status': 'dead', 'last_active': time.time()}
            break

        worker_state[real_pid] = {'status': 'busy', 'last_active': time.time()}
        try:
            # 防御:理论上路由层不会把不匹配的任务送进本池
            if task['func'] != model_type:
                raise RuntimeError(
                    f"Worker 类型 {model_type!r} 收到不匹配的任务 {task['func']!r}（路由错误）"
                )
            # 写到跨进程 results dict(Manager.dict 代理),submit 按 task_id 取自己那条
            res = run_asr_inference(task['path']) if model_type == 'asr' else run_ocr_inference(task['path'])
            results[task['id']] = res
        except KeyboardInterrupt:
            worker_logger.info("[Worker %d] 推理过程中收到 Ctrl+C 信号,中断并退出...", real_pid)
            worker_state[real_pid] = {'status': 'dead', 'last_active': time.time()}
            break
        except Exception as e:
            results[task['id']] = e

        worker_state[real_pid] = {'status': 'idle', 'last_active': time.time()}