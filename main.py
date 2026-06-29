import os
import sys
import traceback
import ipaddress
import socket
import hmac
import logging
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
from urllib.parse import urlparse

# 【生产改造 M1】结构化 logging — 全代码统一 logger
logging.basicConfig(
    level=os.environ.get('FUNASR_LOG_LEVEL', 'INFO'),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
logger = logging.getLogger('funasr')

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

# 【生产改造 C4】路径白名单:默认仅允许上传目录 + 系统临时目录
# 部署时务必通过 -allowed-dirs 覆盖为真实业务目录,例如 /data/uploads
ALLOWED_INPUT_DIRS_DEFAULT = ','.join([
    os.path.expanduser('~/uploads'),
    tempfile.gettempdir(),
])

# 【生产改造 M2】魔法数字提取 — 集中管理便于调优
WORKER_QUEUE_GET_TIMEOUT = 5.0    # worker 进程 task_queue.get 超时(秒)
MONITOR_INTERVAL = 10             # _monitor_workers 扫描间隔(秒)
POLL_INTERVAL = 0.05              # submit 轮询 results 间隔(秒)
HTTP_REQUEST_QUEUE_SIZE = 128     # ThreadingHTTPServer 排队连接数
WAIT_READY_TIMEOUT = 60           # 预热等待就绪超时(秒)
STATS_CACHE_TTL = 1.0             # /metrics stats() 本地缓存(秒)
PREWARN_HEAVY_THRESHOLD = 8       # 预热 worker 数超过此值触发内存/启动时间警告
PREWARM_DEFAULT = 4               # -prewarm 默认值

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

# 【生产改造 C4】路径白名单:__main__ 启动前展开为绝对路径列表
# 改动后会写入,_handle_request 期间只读
_ALLOWED_DIRS = []  # type: list

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
        # 【生产改造 M5】stats() 本地缓存 — 高频 /metrics scrape 时避免跨进程读 Manager.dict
        self._stats_cache = None
        self._stats_cache_time = 0.0

    def start_worker(self):
        # 【生产改造 M4】本方法必须在调用方不持锁时才进(锁外 spawn)
        # 因为 mp.Process.start() 内部阻塞 ~10ms,在锁内会阻塞其他 submit
        if len(self.workers) >= self.max_workers: return

        # 顺便把 os.getpid() 改成 0，因为 worker.py 里已经用 real_pid 覆盖了
        # results 是 Manager.dict 代理,worker 通过它写结果
        p = mp.Process(target=elastic_worker_loop,
                       args=(self.task_queue, self.results, self.worker_state, 0, self.idle_timeout, self.model_type, self.min_workers))
        p.start()
        self.workers[p.pid] = p
        logger.info("[%s Pool] 启动新 Worker (PID: %d),当前 %s 池总数: %d",
                    self.model_type.upper(), p.pid, self.model_type, len(self.workers))

    def wait_ready(self, timeout=WAIT_READY_TIMEOUT):
        """轮询等待本池有 worker 进入 idle 状态(模型加载完成)"""
        name = self.model_type.upper()
        for i in range(timeout):
            time.sleep(1)
            if any(s.get('status') == 'idle' for s in self.worker_state.values()):
                logger.info("✓ %s 池就绪", name)
                return True
            if i > 0 and i % 5 == 0:
                logger.info("%s 池: 已等待 %d 秒...", name, i)
        logger.warning("%s 池等待超时,模型可能加载失败!", name)
        return False

    def submit(self, func_name, path):
        if self.is_shutting_down:
            raise RuntimeError("服务正在关闭，拒绝新请求")

        task_id = uuid.uuid4().hex

        # 【生产改造 M4】need_scale 标记 — start_worker 移到锁外
        try:
            need_scale = False
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
                # 主动扩容:in_flight 接近 alive 时标记,锁外 spawn
                # 不再依赖 busy==alive(那个判断有 ms 级延迟,会漏触发)
                if alive < self.max_workers and self.in_flight >= alive:
                    need_scale = True
                    self.scale_events += 1
                # task dict 只放可序列化的简单数据(无 Queue/Pipe/Lock 等)
                self.task_queue.put({'id': task_id, 'func': func_name, 'path': path})

            if need_scale:
                self.start_worker()  # 锁外 spawn,不阻塞其他 submit

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
                time.sleep(POLL_INTERVAL)  # 50ms 轮询,既不浪费 CPU 也不让用户等太久
        finally:
            # 【生产改造 H2】清理 results,worker 晚到写入也无所谓 — 下次 submit 也会清理
            # 防 TimeoutError 后 worker 仍写回结果造成的内存泄漏
            self.results.pop(task_id, None)
            # 【BUG 修复】无论成功 / 队列满 / 超时 / 关闭,都只递减一次
            with self.lock:
                self.in_flight -= 1

    def stats(self):
        """池状态快照 — 用于 /metrics 端点和日志。线程安全。"""
        # 【生产改造 M5】1 秒本地缓存,避免高频 scrape 时跨进程 Manager.dict 读
        now = time.time()
        if self._stats_cache and now - self._stats_cache_time < STATS_CACHE_TTL:
            return self._stats_cache
        with self.lock:
            # 【BUG 修复】用 items() 一次拿到 (pid, state) 快照,避免两次远程调用之间被改
            state_snapshot = dict(self.worker_state.items())
            states = list(state_snapshot.values())
            alive = sum(1 for p in self.workers.values() if p.is_alive())
            alive_pids = {p.pid for p in self.workers.values() if p.is_alive()}
            # loading:已 spawn 但 worker_state 还没写入(模型加载中)
            loaded_pids = {pid for pid in state_snapshot.keys() if pid in alive_pids}
            loading = alive - len(loaded_pids)
            result = {
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
            self._stats_cache = result
            self._stats_cache_time = now
            return result

    def shutdown(self):
        self.is_shutting_down = True
        logger.info("[Pool] 正在发送退出信号 (毒丸)...")
        for _ in range(self.max_workers):
            self.task_queue.put(None)

        logger.info("[Pool] 等待 Worker 进程退出...")
        with self.lock:
            for pid, p in list(self.workers.items()):
                p.join(timeout=5)
                if p.is_alive():
                    logger.warning("[Pool] Worker (PID: %d) 未响应,强制终止", pid)
                    p.terminate()
                    p.join(timeout=2)
            self.workers.clear()

        # 不在池 shutdown 里关 Manager — Manager 是多池共享的,
        # 第一个池关掉 Manager 会让其他池的 worker 写 worker_state 失败。
        # 交给主进程退出时 OS 回收 Manager 子进程。
        logger.info("[Pool] 所有 Worker 已安全退出。")

    def _monitor_workers(self):
        while True:
            time.sleep(MONITOR_INTERVAL)
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


def _is_safe_url(url: str) -> bool:
    """【生产改造 C3】SSRF 防御:拒绝指向内网/metadata 的 URL
    - scheme 仅允许 http/https
    - getaddrinfo 解析所有 IP,任意一个在内网段就拒
    - 常见 metadata 主机名黑名单
    """
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False
        host = p.hostname
        if not host:
            return False
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False
        for info in set(infos):
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        if host.lower() in {'metadata.google.internal', 'metadata',
                            'kubernetes.default.svc', 'localhost'}:
            return False
        return True
    except Exception:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """【生产改造 C3】禁止 HTTP 30x 重定向,防 30x 跳到内网绕开 SSRF 防护"""
    def http_error_301(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_302(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_303(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_307(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_308(self, req, fp, code, msg, headers): self._block(headers)
    def _block(self, headers):
        raise ValueError(f"HTTP redirect not allowed: {headers.get('Location')}")


def _is_safe_path(filepath: str, allowed_dirs: list) -> str:
    """【生产改造 C4】路径白名单防御:
    - os.path.realpath 解析符号链接和 ..
    - 必须在 allowed_dirs 列表内的某个目录下(允许该目录本身或其子文件)
    - 返回规范化后的绝对路径
    """
    real = os.path.realpath(filepath)
    for allowed in allowed_dirs:
        if real == allowed or real.startswith(allowed + os.sep):
            return real
    raise ValueError(f"Path not allowed (不在白名单目录): {filepath}")


def download_http_file(url: str, suffix: str) -> str:
    if not _is_safe_url(url):
        raise ValueError(f"URL not allowed: {url}")
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        # 用自定义 opener(带 NoRedirect)+ 全局安装,避免污染其他 URL 调用
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(url, timeout=DOWNLOAD_TIMEOUT) as resp:
            content_length = resp.headers.get('Content-Length')
            if content_length and int(content_length) > MAX_CONTENT_LENGTH:
                os.unlink(tmp_path); raise ValueError("文件大小超过限制")
            with open(tmp_path, "wb") as f: shutil.copyfileobj(resp, f)
        return tmp_path
    except Exception:
        if os.path.exists(tmp_path): os.unlink(tmp_path)
        raise

class Handler(BaseHTTPRequestHandler):
    request_queue_size = HTTP_REQUEST_QUEUE_SIZE
    # 【生产改造 C1】API Key 认证:__main__ 启动时注入
    _api_key = None  # type: str | None

    def _check_auth(self) -> bool:
        """【生产改造 C1】校验 X-API-Key Header
        未设置 _api_key → 不校验(开发模式,与默认 127.0.0.1 host 形成双重防御)
        已设置 → 必须 Header 带正确密钥,hmac.compare_digest 防时序攻击
        """
        if not Handler._api_key:
            return True
        return hmac.compare_digest(
            self.headers.get('X-API-Key', ''), Handler._api_key)

    def do_POST(self):
        # 【生产改造 C1】POST 入口先认证
        if not self._check_auth():
            self.send_response(401)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('WWW-Authenticate', 'X-API-Key')
            self.end_headers()
            self.wfile.write(json.dumps({'code': 401, 'message': 'Unauthorized', 'data': None},
                                        ensure_ascii=False).encode('utf-8'))
            return
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
                # 【生产改造 C4】路径白名单校验,防任意文件读取
                real_path = _is_safe_path(filepath, _ALLOWED_DIRS)
                if os.path.getsize(real_path) > MAX_CONTENT_LENGTH:
                    raise ValueError("文件大小超过限制")

            text = target_pool.submit(service_type, real_path)
            duration = time.time() - start_time
            self._json(200, {'code': 200, 'message': '识别成功', 'data': text, 'duration': round(duration, 3)})

        except TimeoutError:
            self._json(408, {'code': 408, 'message': '推理超时', 'data': None})
        except (ValueError, FileNotFoundError) as e:
            # 校验类错误信息对用户调试有用(已被 _is_safe_path 等过滤),但同时记日志
            logger.warning("client error (path=%s): %s", self.path, e)
            self._json(400, {'code': 400, 'message': str(e), 'data': None})
        except Exception as e:
            # 【生产改造 H5】错误脱敏:详细 traceback 入日志,客户端只收通用消息
            if "正在关闭" in str(e):
                self._json(503, {'code': 503, 'message': '服务正在关闭', 'data': None})
            else:
                logger.exception("unhandled error in _handle_request (path=%s, client=%s)",
                                 self.path, self.address_string())
                self._json(500, {'code': 500, 'message': '内部错误,请稍后重试', 'data': None})
        finally:
            if tmp_path and os.path.exists(tmp_path): os.unlink(tmp_path)

    def do_GET(self):
        # 【生产改造 C1】/metrics 走认证(防暴露池容量给侦察)
        if self.path == '/metrics':
            if not self._check_auth():
                self._json(401, {'code': 401, 'message': 'Unauthorized', 'data': None}); return
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
        elif self.path in ['/funasr/health', '/ocr/health']:
            # 【生产改造 H1】健康检查真实化:必须至少 1 个 idle worker 才 200
            # 用于 K8s readiness probe,避免流量进入还没就绪的池子
            model = 'asr' if 'funasr' in self.path else 'ocr'
            pool = pools.get(model)
            if pool is None:
                self._json(503, {'code': 503, 'status': 'pool_not_initialized', 'data': None}); return
            s = pool.stats()
            if s['alive'] == 0 or s['idle'] == 0:
                self._json(503, {'code': 503, 'status': 'not_ready',
                                 'message': 'no idle workers', 'stats': s}); return
            self._json(200, {'code': 200, 'status': 'ok', 'stats': s})
        elif self.path == '/livez':
            # 【生产改造 H1】/livez 永远 200,给 K8s liveness probe 用(避免重启风暴)
            self._json(200, {'code': 200, 'status': 'alive'})
        else:
            self._json(405, {'code': 405, 'message': '仅支持 POST 或 /metrics /health', 'data': None})

    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)


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
    parser.add_argument('-prewarm', type=int, default=PREWARM_DEFAULT, help=f'每池启动时预热的 worker 数(默认 {PREWARM_DEFAULT});设为 20 可全量预热但启动慢且内存高')
    parser.add_argument('-asr-prewarm', type=int, default=None, help='ASR 池预热数;指定后覆盖 -prewarm')
    parser.add_argument('-ocr-prewarm', type=int, default=None, help='OCR 池预热数;指定后覆盖 -prewarm')
    parser.add_argument('-min-workers', type=int, default=1, help='空闲超时后最少保留多少 worker(默认 1);生产推荐设为 -prewarm 同值,避免突发流量冷启动')
    parser.add_argument('-asr-min-workers', type=int, default=None, help='ASR 池保活数;指定后覆盖 -min-workers')
    parser.add_argument('-ocr-min-workers', type=int, default=None, help='OCR 池保活数;指定后覆盖 -min-workers')
    parser.add_argument('-max-queue', type=int, default=200, help='单池最大排队任务数(in_flight - alive 的上限),超过直接 503 防 OOM')
    parser.add_argument('-asr-max-queue', type=int, default=None, help='ASR 池队列上限;指定后覆盖 -max-queue')
    parser.add_argument('-ocr-max-queue', type=int, default=None, help='OCR 池队列上限;指定后覆盖 -max-queue')
    parser.add_argument('-idle', type=int, default=IDLE_TIMEOUT)
    # 【生产改造 C4】路径白名单参数
    parser.add_argument('-allowed-dirs', type=str, default=ALLOWED_INPUT_DIRS_DEFAULT,
                        help='允许的文件路径白名单(逗号分隔绝对路径),展开 ~ 与环境变量')
    # 【生产改造 C1】API Key 认证:任一方式设置即启用,未设置则不强制(开发模式)
    parser.add_argument('-api-key', type=str, default=None,
                        help='API 密钥(启用后客户端必须带 X-API-Key Header,建议用 -api-key-env)')
    parser.add_argument('-api-key-env', type=str, default=None,
                        help='从指定环境变量名读取 API 密钥(避免密钥进 ps)')
    parser.add_argument('-f', type=str, default=None)
    args = parser.parse_args()

    # 【生产改造 C1】解析最终 API key(命令行 > 环境变量)
    _api_key = args.api_key
    if args.api_key_env:
        _api_key = os.environ.get(args.api_key_env) or _api_key
    # 注入到 Handler(类变量,所有请求共享)
    Handler._api_key = _api_key

    # 【生产改造 C4】展开白名单目录为绝对路径列表
    _ALLOWED_DIRS[:] = [
        os.path.realpath(os.path.expanduser(os.path.expandvars(d.strip())))
        for d in args.allowed_dirs.split(',') if d.strip()
    ] if args.allowed_dirs else []  # 空字符串 = 全部拒绝

    if args.f:
        ext = os.path.splitext(args.f)[1].lower()
        if ext in AUDIO_EXTS: service = 'funasr'
        elif ext in IMAGE_EXTS: service = 'ocr'
        else: print('错误: 不支持的文件类型', file=sys.stderr); sys.exit(1)

        base = 'http://%s:%d' % (args.host, args.port)
        # 【生产改造 C1】-f 模式带 X-API-Key Header(已设置 api-key 时)
        cli_headers = {'X-API-Key': _api_key} if _api_key else {}
        try: urllib.request.urlopen(base + '/' + service + '/health', timeout=3)
        except Exception: print('错误: 服务未启动', file=sys.stderr); sys.exit(1)
        req = json.dumps({'filepath': args.f}).encode('utf-8')
        req_obj = urllib.request.Request(base + '/' + service + '/identify', data=req,
                                          headers={'Content-Type': 'application/json', **cli_headers})
        resp = urllib.request.urlopen(req_obj, timeout=300)
        result = json.loads(resp.read().decode('utf-8'))
        if result['code'] == 200: print(result['data'])
        else: print('错误: %s' % result['message'], file=sys.stderr)
    else:
        logger.info("=" * 60)
        logger.info("FunASR & PaddleOCR 弹性伸缩多进程服务 (ASR/OCR 分池)")
        logger.info("=" * 60)

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
                logger.error("❌ %s 池 min_workers(%d) > max_workers(%d),无解配置,退出",
                             name.upper(), cfg['min_workers'], cfg['max_workers'])
                sys.exit(1)
            if cfg['prewarm'] > cfg['max_workers']:
                logger.warning("⚠️  %s 池 prewarm(%d) > max_workers(%d),实际只预热 %d 个",
                                name.upper(), cfg['prewarm'], cfg['max_workers'], cfg['max_workers'])

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
            logger.error("❌ %s", e)
            sys.exit(1)

        # 【生产改造 M7】按 per-pool prewarm 数并行预热(支持 ASR/OCR 各自不同)
        # 每个 worker 进程加载 SenseVoiceSmall + PaddleOCR ≈ 1GB 内存
        total_prewarm = sum(cfg['prewarm'] for cfg in pool_cfg.values())
        est_mem_gb = total_prewarm * 1.0
        prewarm_detail = ' | '.join(
            f"{n.upper()} {cfg['prewarm']} 个" for n, cfg in pool_cfg.items()
        )
        logger.info("正在预热模型(%s,合计 %d 个进程,约 %.0fGB 内存)...",
                    prewarm_detail, total_prewarm, est_mem_gb)
        if total_prewarm > PREWARN_HEAVY_THRESHOLD:
            logger.warning("⚠️  预热 %d 个 worker,启动时间 ≈ %ds,需 %.0fGB 内存",
                           total_prewarm, total_prewarm * 10, est_mem_gb)
        # 并行 spawn:Windows 上 mp.Process.start() 内部 CreateProcess 同步但很快(~100ms),
        # 多 worker 并发比串行快 10x(prewarm=20 时 ~1s → ~100ms)
        def _spawn_one(pool):
            pool.start_worker()
        with ThreadPoolExecutor(max_workers=total_prewarm) as ex:
            futures = []
            for name, cfg in pool_cfg.items():
                pool = pools[name]
                for _ in range(cfg['prewarm']):
                    futures.append(ex.submit(_spawn_one, pool))
            for f in futures:
                f.result()
        with ThreadPoolExecutor(max_workers=len(pools)) as ex:
            futures = [ex.submit(pool.wait_ready) for pool in pools.values()]
            for f in futures:
                f.result()
        logger.info("✓ 双池预热完成,可以接收请求!")

        env_host = '127.0.0.1' if args.host == '0.0.0.0' else args.host
        with open(env_file, 'w') as f:
            f.write('FUNASR_HOST=%s\n' % env_host)
            f.write('FUNASR_PORT=%d\n' % args.port)

        server = ThreadingHTTPServer((args.host, args.port), Handler)

        def graceful_shutdown(signum, frame):
            logger.info("\n\n[Server] 收到退出信号,正在准备优雅关闭...")
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGINT, graceful_shutdown)
        signal.signal(signal.SIGTERM, graceful_shutdown)

        # 弹性配置横幅:用 pools 字典循环输出,加新池自动出现
        pool_lines = ' | '.join(
            f"{p.model_type.upper()} 池 {p.min_workers}-{p.max_workers} 个 Worker(队列上限 {p.max_queue})"
            for p in pools.values()
        )
        logger.info("服务已启动: http://%s:%s", args.host, args.port)
        logger.info("弹性配置: %s | 空闲 %d秒 后缩到最小保活", pool_lines, args.idle)
        logger.info("提示: 支持 Ctrl+C 或 kill 命令优雅退出")

        try:
            server.serve_forever()
        finally:
            logger.info("[Server] 停止接收新请求,正在清理资源...")
            server.server_close()
            for pool in pools.values():
                pool.shutdown()
            if os.path.exists(env_file): os.unlink(env_file)
            logger.info("[Server] 服务已完全停止,所有资源已释放。")