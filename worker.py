import os
import sys
import time
import queue

# worker 用 get_exe_dir:模型文件是用户放在 exe 旁边的,不在 _MEIPASS
from _paths import get_exe_dir

BASE_DIR = get_exe_dir()


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
    print(f"[Worker {pid}] 正在加载 {model_type.upper()} 模型...")

    if model_type == 'asr':
        from funasr_onnx import SenseVoiceSmall
        model_dir = os.environ.get("FUNASR_MODEL_DIR", os.path.join(BASE_DIR, "model"))
        _asr_model = SenseVoiceSmall(model_dir, batch_size=1, quantize=True, intra_op_num_threads=1)
        print(f"[Worker {pid}] ✓ ASR 模型加载完成")
    elif model_type == 'ocr':
        # 【核心修复】：在 Windows 下，使用 Monkey Patch 强制关闭底层的 IR 优化
        if sys.platform == 'win32':
            import paddle.inference
            original_switch_ir_optim = paddle.inference.Config.switch_ir_optim
            def fake_switch_ir_optim(self, enable):
                return original_switch_ir_optim(self, False)
            paddle.inference.Config.switch_ir_optim = fake_switch_ir_optim

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
        print(f"[Worker {pid}] ✓ OCR 模型加载完成 (OneDNN: {'OFF' if is_win else 'ON'})")
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
def elastic_worker_loop(task_queue, result_queue, worker_state, pid_placeholder, idle_timeout, model_type):
    # 【关键修复 1】：在子进程内部获取真实的 PID，覆盖掉主进程传来的占位符 0
    real_pid = os.getpid()

    # 【关键修复 2】：增加 try/except，防止模型加载失败时静默崩溃
    try:
        init_worker_processes(model_type)
        worker_state[real_pid] = {'status': 'idle', 'last_active': time.time()}
    except Exception as e:
        print(f"[Worker {real_pid}] 模型加载失败: {e}")
        worker_state[real_pid] = {'status': 'dead', 'last_active': time.time()}
        return  # 直接退出，让主进程的监控线程去清理

    while True:
        try:
            task = task_queue.get(timeout=5.0)
        except queue.Empty:
            state = worker_state.get(real_pid)
            if state and (time.time() - state['last_active'] > idle_timeout):
                alive_count = sum(1 for s in worker_state.values() if s.get('status') != 'dead')
                if alive_count > 1:
                    # 【注意】"至少保留 1 个 alive"是 best-effort:两个 worker 几乎同时
                    # 走到这里时都可能看到 alive_count>1 而双双退出。系统会自愈
                    # (下一个 submit 看到 alive_pids 为空,触发 start_worker)。
                    print(f"[Worker {real_pid}] 空闲超时，主动退出以释放资源。")
                    worker_state[real_pid] = {'status': 'dead', 'last_active': time.time()}
                    break
            continue
        except KeyboardInterrupt:
            print(f"[Worker {real_pid}] 收到 Ctrl+C 信号，正在优雅退出...")
            worker_state[real_pid] = {'status': 'dead', 'last_active': time.time()}
            break

        if task is None:
            print(f"[Worker {real_pid}] 收到退出信号 (毒丸)，正在优雅退出...")
            worker_state[real_pid] = {'status': 'dead', 'last_active': time.time()}
            break

        worker_state[real_pid] = {'status': 'busy', 'last_active': time.time()}
        try:
            # 防御:理论上路由层不会把不匹配的任务送进本池
            if task['func'] != model_type:
                raise RuntimeError(
                    f"Worker 类型 {model_type!r} 收到不匹配的任务 {task['func']!r}（路由错误）"
                )
            res = run_asr_inference(task['path']) if model_type == 'asr' else run_ocr_inference(task['path'])
            result_queue.put((task['id'], res))
        except KeyboardInterrupt:
            print(f"[Worker {real_pid}] 推理过程中收到 Ctrl+C 信号，中断并退出...")
            worker_state[real_pid] = {'status': 'dead', 'last_active': time.time()}
            break
        except Exception as e:
            result_queue.put((task['id'], e))

        worker_state[real_pid] = {'status': 'idle', 'last_active': time.time()}