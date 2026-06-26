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

# 【生产改造】Windows 默认 GBK 控制台无法编码 ✓(U+2713)等 Unicode
# worker 子进程继承环境变量,这里必须提前设,否则 print ✓ 直接抛异常
os.environ['PYTHONIOENCODING'] = 'utf-8'
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

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
from _paths import get_pkg_dir, get_exe_dir, setup_bundled_env

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


# frozen 时把 _MEIPASS/bin 注入 PATH + 设 TORCHAUDIO_USE_FFMPEG_PATH
# 注意:ccache 是静态二进制,无 .so,不需要进 LD_LIBRARY_PATH(否则未来同名 .so 会被误加载)
setup_bundled_env()

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
    def __init__(self, model_type, max_workers, idle_timeout, max_queue=200, min_workers=1):
        self.model_type = model_type
        self.max_workers = max_workers
        self.idle_timeout = idle_timeout
        self.max_queue = max_queue  # 任务队列上限,防 OOM
        # 【生产改造】最小保活 worker 数,空闲超时后保留多少个不再缩容
        # 生产推荐设 = -prewarm,避免突发流量后冷启动
        self.min_workers = min_workers
        self.task_queue = mp.Queue()
        # Manager.dict 作为跨进程结果通道:worker 写 results[task_id]=res,
        # submit 轮询自己的 task_id 拿到结果。
        # 不能用 mp.Queue + task dict 传递:mp.Queue.__getstate__ 限制 Queue
        # 只能通过 Process(args=...) 直接传,不能通过其他 Queue 间接传(spawn 下)
        self.manager = get_shared_manager()
        self.worker_state = self.manager.dict()
        self.results = self.manager.dict()
        self.workers = {}
        self.lock = threading.Lock()
        self.is_shutting_down = False
        # 【生产改造】in_flight:当前在飞任务数(已派发未拿结果)
        # 比 busy 状态更准——worker 标 busy 有 ms 级延迟,in_flight 立即可见
        # 用它做主动扩容:in_flight >= alive 时立刻 spawn 新 worker
        self.in_flight = 0
        self.scale_events = 0  # 扩容次数,metrics 观测用

        self.monitor_thread = threading.Thread(target=self._monitor_workers, daemon=True)
        self.monitor_thread.start()

    def start_worker(self):
        # 【关键修复】：去掉内部的 with self.lock:！
        # 因为调用此方法的 submit() 已经持有了锁，嵌套获取会导致死锁。
        if len(self.workers) >= self.max_workers: return

        # 顺便把 os.getpid() 改成 0，因为 worker.py 里已经用 real_pid 覆盖了
        # results 是 Manager.dict 代理,worker 通过它写结果
        p = mp.Process(target=elastic_worker_loop,
                       args=(self.task_queue, self.results, self.worker_state, 0, self.idle_timeout, self.model_type, self.min_workers))
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

        try:
            with self.lock:
                # 【生产改造】先占 in_flight 再判断扩容——
                # 这样并发提交时每个请求都立刻看到自己在飞,
                # 触发扩容不等 worker 异步标 busy
                self.in_flight += 1
                alive = sum(1 for p in self.workers.values() if p.is_alive())
                # 队列过载保护:in_flight 上限 = alive + max_queue
                # 满了直接拒,不等超时
                if self.in_flight > alive + self.max_queue:
                    # 【BUG 修复】不再这里 -=1 — finally 会统一递减
                    # 之前双递减会导致实际限速比 alive+max_queue 少 1
                    raise RuntimeError(f"队列已满({alive} worker / {self.max_queue} 排队上限)")
                # 主动扩容:in_flight 接近 alive 时立即 spawn 新 worker
                # 不再依赖 busy==alive(那个判断有 ms 级延迟,会漏触发)
                if alive < self.max_workers and self.in_flight >= alive:
                    self.start_worker()
                    self.scale_events += 1
                # task dict 只放可序列化的简单数据(无 Queue/Pipe/Lock 等)
                self.task_queue.put({'id': task_id, 'func': func_name, 'path': path})

            # 轮询 self.results 等 worker 写入,task_id 唯一所以不会拿错
            start_time = time.time()
            while True:
                if self.is_shutting_down:
                    raise RuntimeError("服务正在关闭，推理被中断")
                if task_id in self.results:
                    data = self.results.pop(task_id)
                    if isinstance(data, Exception): raise data
                    return data
                if time.time() - start_time > INFERENCE_TIMEOUT:
                    raise TimeoutError("推理超时")
                time.sleep(0.05)  # 50ms 轮询,既不浪费 CPU 也不让用户等太久
        finally:
            # 【BUG 修复】无论成功 / 队列满 / 超时 / 关闭,都只递减一次
            with self.lock:
                self.in_flight -= 1

    def stats(self):
        """池状态快照 — 用于 /metrics 端点和日志。线程安全。"""
        with self.lock:
            # 【BUG 修复】用 items() 一次拿到 (pid, state) 快照,避免两次远程调用之间被改
            state_snapshot = dict(self.worker_state.items())
            states = list(state_snapshot.values())
            alive = sum(1 for p in self.workers.values() if p.is_alive())
            alive_pids = {p.pid for p in self.workers.values() if p.is_alive()}
            # loading:已 spawn 但 worker_state 还没写入(模型加载中)
            loaded_pids = {pid for pid in state_snapshot.keys() if pid in alive_pids}
            loading = alive - len(loaded_pids)
            return {
                'model_type': self.model_type,
                'alive': alive,
                'max': self.max_workers,
                'min': self.min_workers,
                'in_flight': self.in_flight,
                'idle': sum(1 for s in states if s.get('status') == 'idle'),
                'busy': sum(1 for s in states if s.get('status') == 'busy'),
                'loading': loading,
                'dead': sum(1 for s in states if s.get('status') == 'dead'),
                'scale_events': self.scale_events,
            }

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
                # 启动时已通过 preflight_check_models 校验过模型文件存在,
                # 所以这里 worker 死掉通常是运行时问题(OOM、bug 等),系统自愈:
                # 下次 submit 看到 alive 不足会触发 start_worker。

# ================= 辅助函数与 HTTP 服务器 =================
def preflight_check_models(pools: dict):
    """启动前校验每个池需要的模型文件/目录,缺则 raise FileNotFoundError。

    这样在 worker 反复 init 失败 300s 超时之前就 fail-fast,
    错误信息直接告诉用户缺什么、放在哪、怎么覆盖路径。
    """
    exe_dir = get_exe_dir()
    for model_type in pools:
        if model_type == 'asr':
            model_dir = os.environ.get("FUNASR_MODEL_DIR", os.path.join(exe_dir, "model"))
            # 必需文件:对应 worker.py 的 SenseVoiceSmall(model_dir, quantize=True) 加载路径
            required = ['model_quant.onnx', 'tokens.json', 'config.yaml']
            missing = [f for f in required if not os.path.isfile(os.path.join(model_dir, f))]
            if missing:
                raise FileNotFoundError(
                    f"ASR 模型文件缺失: {model_dir}/{', '.join(missing)}\n"
                    f"  下载:见 README 中的 wget 命令\n"
                    f"  或设置环境变量 FUNASR_MODEL_DIR 指向已就绪的模型目录"
                )
        elif model_type == 'ocr':
            ocr_dir = os.environ.get("FUNASR_OCR_MODEL_DIR", os.path.join(exe_dir, "model", "paddleocr"))
            # 必需子目录:对应 worker.py 的 PaddleOCR(det/rec/cls model_dir) 加载路径
            required = ['det', 'rec', 'cls']
            missing = [d for d in required if not os.path.isdir(os.path.join(ocr_dir, d))]
            if missing:
                raise FileNotFoundError(
                    f"OCR 模型目录缺失: {ocr_dir}/{', '.join(missing)}/\n"
                    f"  下载:见 README 中的 curl/tar 命令\n"
                    f"  或设置环境变量 FUNASR_OCR_MODEL_DIR 指向已就绪的目录"
                )


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
        # 【生产改造】拒绝条件收窄:只在"完全无可用 worker"时才拒
        # busy 不再是拒绝理由 — busy 时请求进 submit() 排队 + 触发主动扩容
        s = target_pool.stats()
        if s['alive'] == 0 or s['alive'] == s['dead']:
            self._json(503, {'code': 503, 'message': '服务正在启动/无可用 worker，请稍后重试', 'data': None})
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
        elif self.path == '/metrics':
            # Prometheus 文本格式 — 每池一行,带 model 标签
            lines = []
            for pool in pools.values():
                s = pool.stats()
                model = s['model_type']
                for key in ('alive', 'max', 'min', 'in_flight', 'idle', 'busy', 'loading', 'dead', 'scale_events'):
                    lines.append(f'funasr_pool_{key}{{model="{model}"}} {s[key]}')
            body = ('\n'.join(lines) + '\n').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(405, {'code': 405, 'message': '仅支持 POST 或 /metrics /health', 'data': None})

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
    parser.add_argument('-workers', type=int, default=16, help='ASR 与 OCR 池各自的最大 worker 数(默认 16);支持 -workers 20 提升到 20 并发')
    parser.add_argument('-asr-workers', type=int, default=None, help='ASR 池最大 worker 数；指定后覆盖 -workers')
    parser.add_argument('-ocr-workers', type=int, default=None, help='OCR 池最大 worker 数；指定后覆盖 -workers')
    parser.add_argument('-prewarm', type=int, default=4, help='每池启动时预热的 worker 数(默认 4);设为 20 可全量预热但启动慢且内存高')
    parser.add_argument('-asr-prewarm', type=int, default=None, help='ASR 池预热数;指定后覆盖 -prewarm')
    parser.add_argument('-ocr-prewarm', type=int, default=None, help='OCR 池预热数;指定后覆盖 -prewarm')
    parser.add_argument('-min-workers', type=int, default=1, help='空闲超时后最少保留多少 worker(默认 1);生产推荐设为 -prewarm 同值,避免突发流量冷启动')
    parser.add_argument('-asr-min-workers', type=int, default=None, help='ASR 池保活数;指定后覆盖 -min-workers')
    parser.add_argument('-ocr-min-workers', type=int, default=None, help='OCR 池保活数;指定后覆盖 -min-workers')
    parser.add_argument('-max-queue', type=int, default=200, help='单池最大排队任务数(in_flight - alive 的上限),超过直接 503 防 OOM')
    parser.add_argument('-asr-max-queue', type=int, default=None, help='ASR 池队列上限;指定后覆盖 -max-queue')
    parser.add_argument('-ocr-max-queue', type=int, default=None, help='OCR 池队列上限;指定后覆盖 -max-queue')
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
        print('=' * 60)
        print('FunASR & PaddleOCR 弹性伸缩多进程服务 (ASR/OCR 分池)')
        print('=' * 60)

        # 【生产改造】per-pool 参数解析:优先 asr/ocr 独立值,缺省用全局值
        # 例: -prewarm 4 -asr-prewarm 8 → ASR 池预热 8,OCR 池预热 4
        pool_cfg = {
            'asr': {
                'max_workers': args.asr_workers or args.workers,
                'prewarm':     args.asr_prewarm if args.asr_prewarm is not None else args.prewarm,
                'min_workers': args.asr_min_workers if args.asr_min_workers is not None else args.min_workers,
                'max_queue':   args.asr_max_queue if args.asr_max_queue is not None else args.max_queue,
            },
            'ocr': {
                'max_workers': args.ocr_workers or args.workers,
                'prewarm':     args.ocr_prewarm if args.ocr_prewarm is not None else args.prewarm,
                'min_workers': args.ocr_min_workers if args.ocr_min_workers is not None else args.min_workers,
                'max_queue':   args.ocr_max_queue if args.ocr_max_queue is not None else args.max_queue,
            },
        }

        # 【校验】min_workers 不能大于 max_workers,否则池永远不会缩容(逻辑死循环)
        for name, cfg in pool_cfg.items():
            if cfg['min_workers'] > cfg['max_workers']:
                print(f"❌ {name.upper()} 池 min_workers({cfg['min_workers']}) > max_workers({cfg['max_workers']}),无解配置,退出",
                      file=sys.stderr)
                sys.exit(1)
            if cfg['prewarm'] > cfg['max_workers']:
                print(f"⚠️  {name.upper()} 池 prewarm({cfg['prewarm']}) > max_workers({cfg['max_workers']}),实际只预热 {cfg['max_workers']} 个")

        # 用 pools 字典统一管理:加新模型只需在 ROUTES + 此处加一行
        pools.update({
            name: ElasticProcessPool(
                model_type=name,
                max_workers=cfg['max_workers'],
                idle_timeout=args.idle,
                max_queue=cfg['max_queue'],
                min_workers=cfg['min_workers'],
            )
            for name, cfg in pool_cfg.items()
        })

        # 启动前 fail-fast:校验所有池的模型文件,缺则直接退出,避免 worker 反复
        # init 失败、每次 submit 等 300s 超时的恢复循环
        try:
            preflight_check_models(pools)
        except FileNotFoundError as e:
            print(f"\n❌ {e}\n", file=sys.stderr)
            sys.exit(1)

        # 【生产改造】按 per-pool prewarm 数预热(支持 ASR/OCR 各自不同)
        # 每个 worker 进程加载 SenseVoiceSmall + PaddleOCR ≈ 1GB 内存
        total_prewarm = sum(cfg['prewarm'] for cfg in pool_cfg.values())
        est_mem_gb = total_prewarm * 1.0
        prewarm_detail = ' | '.join(
            f"{n.upper()} {cfg['prewarm']} 个" for n, cfg in pool_cfg.items()
        )
        print(f"正在预热模型({prewarm_detail},合计 {total_prewarm} 个进程,约 {est_mem_gb:.0f}GB 内存)...")
        if total_prewarm > 8:
            print(f"⚠️  预热 {total_prewarm} 个 worker,启动时间 ≈ {total_prewarm * 10}s,需 {est_mem_gb:.0f}GB 内存")
        for name, cfg in pool_cfg.items():
            pool = pools[name]
            for _ in range(cfg['prewarm']):
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
        pool_lines = ' | '.join(
            f"{p.model_type.upper()} 池 {p.min_workers}-{p.max_workers} 个 Worker(队列上限 {p.max_queue})"
            for p in pools.values()
        )
        print(f'\n服务已启动: http://{args.host}:{args.port}')
        print(f'弹性配置: {pool_lines} | 空闲 {args.idle}秒 后缩到最小保活')
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