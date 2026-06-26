import os
import sys
import traceback
import json
import time
import shutil
import argparse
import tempfile
import threading
import urllib.request
import multiprocessing as mp
import queue
import uuid
import warnings
import signal

# ================= 基础环境与警告屏蔽 =================
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# 【PyInstaller 关键】：多进程支持必须放在最前面
mp.freeze_support()
try:
    mp.set_start_method('spawn', force=True)
except RuntimeError:
    pass

os.environ['FLAGS_cpu_math_library_num_threads'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'

if sys.platform == 'win32':
    os.environ['FLAGS_use_mkldnn'] = '0'
    os.environ['FLAGS_use_onednn'] = '0'

# ================= 路径与环境兼容 =================
from _paths import get_pkg_dir, get_exe_dir

# BASE_DIR 是打包资源目录(_MEIPASS),用于 paddle/libs、bundled bin 等
BASE_DIR = get_pkg_dir()


def prepend_env(name, value):
    """把 value 拼到环境变量 name 的最前面(用 os.pathsep 分隔)"""
    os.environ[name] = value + os.pathsep + os.environ.get(name, '')


# mp.Manager() 每个进程内单例 — 避免 N 个池起 N 个 Manager 服务进程
_shared_manager = None
def get_shared_manager():
    global _shared_manager
    if _shared_manager is None:
        _shared_manager = mp.Manager()
    return _shared_manager


# 把打包进二进制的 ffmpeg / ccache 目录加到 PATH 最前
# 这样 torchaudio 探测 ffmpeg、PaddlePaddle 调用 which ccache 都能命中,不再打印告警
# 注意:ccache 是静态二进制,无 .so,不需要进 LD_LIBRARY_PATH(否则未来同名 .so 会被误加载)
if getattr(sys, 'frozen', False):
    _bundled_bin = os.path.join(BASE_DIR, 'bin')
    if os.path.isdir(_bundled_bin):
        prepend_env('PATH', _bundled_bin)

if sys.platform == 'win32':
    for path in [os.path.join(BASE_DIR, 'torch', 'lib'), os.path.join(BASE_DIR, 'Library', 'bin')]:
        if os.path.exists(path):
            try: os.add_dll_directory(path)
            except Exception: pass

if sys.platform.startswith('linux') and getattr(sys, 'frozen', False):
    import site as _site
    import pathlib as _pathlib
    _base = _pathlib.Path(BASE_DIR)
    _site.getsitepackages = lambda: [str(_base)]
    _site.USER_SITE = str(_base)
    for _sub in ['paddle/libs', 'paddle/base']:
        _pp = _base / _sub
        if _pp.exists():
            prepend_env('LD_LIBRARY_PATH', str(_pp))

# ================= 生产环境配置 =================
MAX_CONTENT_LENGTH = 100 * 1024 * 1024
INFERENCE_TIMEOUT = 300
DOWNLOAD_TIMEOUT = 60
IDLE_TIMEOUT = 300

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor


# 支持的文件后缀
AUDIO_EXTS = {'.wav', '.mp3', '.m4a', '.flac', '.ogg', '.aac', '.wma', '.opus', '.ape', '.ac3'}
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff', '.webp', '.tif', '.jfif'}

# 路由表:HTTP path 前缀 → 模型类型。__main__ 会按 model_type 找到对应池。
ROUTES = {
    '/funasr/identify': 'asr',
    '/ocr/identify': 'ocr',
}
# 池注册表:model_type → ElasticProcessPool。__main__ 启动时填充。
pools: dict = {}

# 【关键修复】：从独立的 worker 模块导入循环函数，彻底解决 PyInstaller 打包报错
from worker import elastic_worker_loop 

# ================= 弹性进程池管理器 =================
class ElasticProcessPool:
    def __init__(self, model_type, max_workers, idle_timeout):
        self.model_type = model_type
        self.max_workers = max_workers
        self.idle_timeout = idle_timeout
        self.task_queue = mp.Queue()
        # 注意:不再用共享 result_queue — 改为每个 submit 自建 result_q,
        # 避免多线程 HTTP server 下不同 submit 互相抢读、丢弃对方的结果
        self.manager = get_shared_manager()
        self.worker_state = self.manager.dict()
        self.workers = {}
        self.lock = threading.Lock()
        self.is_shutting_down = False

        self.monitor_thread = threading.Thread(target=self._monitor_workers, daemon=True)
        self.monitor_thread.start()

    def start_worker(self):
        # 【关键修复】：去掉内部的 with self.lock:！
        # 因为调用此方法的 submit() 已经持有了锁，嵌套获取会导致死锁。
        if len(self.workers) >= self.max_workers: return

        # 顺便把 os.getpid() 改成 0，因为 worker.py 里已经用 real_pid 覆盖了
        # 不再传 result_queue,worker 通过 task['result_q'] 拿专属回写通道
        p = mp.Process(target=elastic_worker_loop,
                       args=(self.task_queue, self.worker_state, 0, self.idle_timeout, self.model_type))
        p.start()
        self.workers[p.pid] = p
        print(f"[{self.model_type.upper()} Pool] 启动新 Worker (PID: {p.pid})，当前 {self.model_type} 池总数: {len(self.workers)}")

    def wait_ready(self, timeout=60):
        """轮询等待本池有 worker 进入 idle 状态(模型加载完成)"""
        name = self.model_type.upper()
        for i in range(timeout):
            time.sleep(1)
            if any(s.get('status') == 'idle' for s in self.worker_state.values()):
                print(f"  ✓ {name} 池就绪")
                return True
            if i > 0 and i % 5 == 0:
                print(f"  {name} 池: 已等待 {i} 秒...")
        print(f"警告: {name} 池等待超时，模型可能加载失败！")
        return False

    def submit(self, func_name, path):
        if self.is_shutting_down:
            raise RuntimeError("服务正在关闭，拒绝新请求")

        task_id = uuid.uuid4().hex
        # 每个 submit 自建 result_q,worker 处理完后写到这条专属队列
        # 这样多线程 HTTP server 下不同 submit 不会抢结果
        result_q = mp.Queue()
        self.task_queue.put({'id': task_id, 'func': func_name, 'path': path, 'result_q': result_q})

        with self.lock:
            alive_pids = [pid for pid in self.workers if self.workers[pid].is_alive()]
            busy_pids = [pid for pid in alive_pids if self.worker_state.get(pid, {}).get('status') == 'busy']
            if len(busy_pids) == len(alive_pids) and len(alive_pids) < self.max_workers:
                self.start_worker()

        start_time = time.time()
        while True:
            if self.is_shutting_down:
                raise RuntimeError("服务正在关闭，推理被中断")
            try:
                res_data = result_q.get(timeout=1.0)
                if isinstance(res_data, Exception): raise res_data
                return res_data
            except queue.Empty:
                if time.time() - start_time > INFERENCE_TIMEOUT:
                    raise TimeoutError("推理超时")

    def shutdown(self):
        self.is_shutting_down = True
        print("[Pool] 正在发送退出信号 (毒丸)...")
        for _ in range(self.max_workers):
            self.task_queue.put(None)

        print("[Pool] 等待 Worker 进程退出...")
        with self.lock:
            for pid, p in list(self.workers.items()):
                p.join(timeout=5)
                if p.is_alive():
                    print(f"[Pool] Worker (PID: {pid}) 未响应，强制终止。")
                    p.terminate()
                    p.join(timeout=2)
            self.workers.clear()

        # 不在池 shutdown 里关 Manager — Manager 是多池共享的,
        # 第一个池关掉 Manager 会让其他池的 worker 写 worker_state 失败。
        # 交给主进程退出时 OS 回收 Manager 子进程。
        print("[Pool] 所有 Worker 已安全退出。")

    def _monitor_workers(self):
        while True:
            time.sleep(10)
            with self.lock:
                dead_pids = [pid for pid in self.workers if not self.workers[pid].is_alive()]
                for pid in dead_pids:
                    self.workers.pop(pid, None)
                    self.worker_state.pop(pid, None)

# ================= 辅助函数与 HTTP 服务器 =================
def download_http_file(url: str, suffix: str) -> str:
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:
            content_length = resp.headers.get('Content-Length')
            if content_length and int(content_length) > MAX_CONTENT_LENGTH:
                os.unlink(tmp_path); raise ValueError("文件大小超过限制")
            with open(tmp_path, "wb") as f: shutil.copyfileobj(resp, f)
        return tmp_path
    except Exception:
        if os.path.exists(tmp_path): os.unlink(tmp_path)
        raise

class Handler(BaseHTTPRequestHandler):
    request_queue_size = 128

    def do_POST(self):
        model_type = ROUTES.get(self.path)
        if model_type is None:
            self._json(404, {'code': 404, 'message': '未找到路由', 'data': None})
            return
        target_pool = pools.get(model_type)
        if target_pool is None:
            self._json(503, {'code': 503, 'message': f'{model_type.upper()} 池未初始化', 'data': None})
            return
        self._handle_request(model_type, target_pool)

    def _handle_request(self, service_type, target_pool):
        # 没有 idle worker 时拒绝(冷启动或全在 busy 之外的 dead/initializing)
        if not any(s.get('status') == 'idle' for s in target_pool.worker_state.values()):
            self._json(503, {'code': 503, 'message': '服务正在启动/模型加载中，请稍后重试', 'data': None})
            return

        tmp_path = None
        try:
            start_time = time.time()
            length = int(self.headers.get('Content-Length', 0))
            if length > MAX_CONTENT_LENGTH:
                self._json(413, {'code': 413, 'message': '请求体过大', 'data': None}); return

            body = json.loads(self.rfile.read(length).decode('utf-8'))
            filepath = body.get('filepath')
            if not filepath:
                self._json(400, {'code': 400, 'message': '缺少 filepath 参数', 'data': None}); return

            # 验证文件后缀
            ext = os.path.splitext(filepath)[1].lower()
            if service_type == 'asr' and ext not in AUDIO_EXTS:
                self._json(400, {'code': 400, 'message': f'ASR 端点不支持文件类型: {ext}，支持的格式: {", ".join(sorted(AUDIO_EXTS))}', 'data': None}); return
            if service_type == 'ocr' and ext not in IMAGE_EXTS:
                self._json(400, {'code': 400, 'message': f'OCR 端点不支持文件类型: {ext}，支持的格式: {", ".join(sorted(IMAGE_EXTS))}', 'data': None}); return

            if filepath.startswith(("http://", "https://")):
                suffix = "_audio" if service_type == "asr" else "_image"
                tmp_path = download_http_file(filepath, suffix)
                real_path = tmp_path
            else:
                real_path = filepath
                if os.path.getsize(real_path) > MAX_CONTENT_LENGTH:
                    raise ValueError("文件大小超过限制")

            text = target_pool.submit(service_type, real_path)
            duration = time.time() - start_time
            self._json(200, {'code': 200, 'message': '识别成功', 'data': text, 'duration': round(duration, 3)})

        except TimeoutError:
            self._json(408, {'code': 408, 'message': '推理超时', 'data': None})
        except (ValueError, FileNotFoundError) as e:
            self._json(400, {'code': 400, 'message': str(e), 'data': None})
        except Exception as e:
            status_code = 503 if "正在关闭" in str(e) else 500
            self._json(status_code, {'code': status_code, 'message': str(e) or '系统繁忙', 'data': None})
        finally:
            if tmp_path and os.path.exists(tmp_path): os.unlink(tmp_path)

    def do_GET(self):
        if self.path in ['/funasr/health', '/ocr/health']:
            self._json(200, {'code': 200, 'status': 'ok'})
        else:
            self._json(405, {'code': 405, 'message': '仅支持 POST', 'data': None})

    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print("%s - %s" % (self.address_string(), format % args))


if __name__ == '__main__':
    # .env 放在 exe 旁边(用户可见位置),不用 _MEIPASS(临时目录)
    base_dir = get_exe_dir()
    env_file = os.path.join(base_dir, '.env')

    def read_env():
        host, port = None, None
        if os.path.exists(env_file):
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('FUNASR_HOST='): host = line.split('=', 1)[1]
                    elif line.startswith('FUNASR_PORT='): port = int(line.split('=', 1)[1])
        return host, port

    env_host, env_port = read_env()
    parser = argparse.ArgumentParser()
    parser.add_argument('-host', default=env_host or '127.0.0.1')
    parser.add_argument('-port', type=int, default=env_port or 5001)
    parser.add_argument('-workers', type=int, default=16, help='ASR 与 OCR 池各自的最大 worker 数（默认 16）')
    parser.add_argument('-asr-workers', type=int, default=None, help='ASR 池最大 worker 数；指定后覆盖 -workers')
    parser.add_argument('-ocr-workers', type=int, default=None, help='OCR 池最大 worker 数；指定后覆盖 -workers')
    parser.add_argument('-idle', type=int, default=IDLE_TIMEOUT)
    parser.add_argument('-f', type=str, default=None)
    args = parser.parse_args()

    if args.f:
        ext = os.path.splitext(args.f)[1].lower()
        if ext in AUDIO_EXTS: service = 'funasr'
        elif ext in IMAGE_EXTS: service = 'ocr'
        else: print('错误: 不支持的文件类型', file=sys.stderr); sys.exit(1)

        base = 'http://%s:%d' % (args.host, args.port)
        try: urllib.request.urlopen(base + '/' + service + '/health', timeout=3)
        except Exception: print('错误: 服务未启动', file=sys.stderr); sys.exit(1)
        req = json.dumps({'filepath': args.f}).encode('utf-8')
        resp = urllib.request.urlopen(base + '/' + service + '/identify', data=req, timeout=300)
        result = json.loads(resp.read().decode('utf-8'))
        if result['code'] == 200: print(result['data'])
        else: print('错误: %s' % result['message'], file=sys.stderr)
    else:
        # -asr-workers / -ocr-workers 显式指定时优先,否则继承 -workers
        asr_max = args.asr_workers or args.workers
        ocr_max = args.ocr_workers or args.workers

        print('=' * 60)
        print('FunASR & PaddleOCR 弹性伸缩多进程服务 (ASR/OCR 分池)')
        print('=' * 60)

        # 用 pools 字典统一管理:加新模型只需在 ROUTES + 此处加一行
        pools.update({
            'asr': ElasticProcessPool(model_type='asr', max_workers=asr_max, idle_timeout=args.idle),
            'ocr': ElasticProcessPool(model_type='ocr', max_workers=ocr_max, idle_timeout=args.idle),
        })

        # 各池预热 1 个 worker,然后并行等就绪(总等待 = max(各池) 而非 sum)
        print("正在预热 ASR 与 OCR 模型 (各 1 个)...")
        for pool in pools.values():
            pool.start_worker()
        with ThreadPoolExecutor(max_workers=len(pools)) as ex:
            futures = [ex.submit(pool.wait_ready) for pool in pools.values()]
            for f in futures:
                f.result()
        print("✓ 双池预热完成，可以接收请求！")

        env_host = '127.0.0.1' if args.host == '0.0.0.0' else args.host
        with open(env_file, 'w') as f:
            f.write('FUNASR_HOST=%s\n' % env_host)
            f.write('FUNASR_PORT=%d\n' % args.port)

        server = ThreadingHTTPServer((args.host, args.port), Handler)

        def graceful_shutdown(signum, frame):
            print('\n\n[Server] 收到退出信号，正在准备优雅关闭...')
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGINT, graceful_shutdown)
        signal.signal(signal.SIGTERM, graceful_shutdown)

        # 弹性配置横幅:用 pools 字典循环输出,加新池自动出现
        pool_lines = ' | '.join(f"{p.model_type.upper()} 池 1-{p.max_workers} 个 Worker" for p in pools.values())
        print(f'\n服务已启动: http://{args.host}:{args.port}')
        print(f'弹性配置: {pool_lines} | 空闲 {args.idle}秒 后缩容')
        print('提示: 支持 Ctrl+C 或 kill 命令优雅退出。按 Ctrl+C 停止服务\n')

        try:
            server.serve_forever()
        finally:
            print("[Server] 停止接收新请求，正在清理资源...")
            server.server_close()
            for pool in pools.values():
                pool.shutdown()
            if os.path.exists(env_file): os.unlink(env_file)
            print("[Server] 服务已完全停止，所有资源已释放。")