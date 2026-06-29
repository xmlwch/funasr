import os
import sys
import glob  # 【生产改造 task29】glob 通配符展开(M-A)
import logging
import json
import argparse
import tempfile
import threading
import urllib.request
import multiprocessing as mp
import warnings
import signal

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


def _expand_allowed_dirs(dirs_str: str, max_results: int = 1000) -> list:
    """【生产改造 task29】展开 -allowed-dirs,支持 ~ $VAR 和 glob 通配符

    模式语法(按 glob 标准):
      字面路径      /data/uploads          → 1 个具体路径
      单层通配符    ~/uploads/*           → 匹配 ~/uploads 直接子项
      字符通配符    ~/uploads/202?-*/log  → ? 单字符 + [seq] 字符集
      递归通配符    ~/uploads/**          → 匹配所有后代(需显式 ** 才递归)
      任意组合      $DATA_ROOT/2024-*/*   → 环境变量 + 通配

    安全护栏:
      - 展开结果 > max_results → raise ValueError(防 DoS)
      - 无匹配 → logger.warning(不报错,允许配置未来的目录)
      - 字面路径 → 直接保留,不调 glob

    Returns:
        list[str] 展开后的路径列表(含展开失败的模式跳过)
    """
    raw = [d.strip() for d in dirs_str.split(',') if d.strip()]
    expanded = []
    for pattern in raw:
        # ~ 与 $VAR 展开
        expanded_pattern = os.path.expanduser(os.path.expandvars(pattern))
        # 无通配符当字面路径处理
        if not any(c in expanded_pattern for c in '*?['):
            expanded.append(expanded_pattern)
            continue
        # ** 触发递归,glob 默认 recursive=False
        recursive = '**' in expanded_pattern
        matches = glob.glob(expanded_pattern, recursive=recursive)
        if not matches:
            logger.warning("-allowed-dirs 模式无匹配: %s (展开: %s)",
                           pattern, expanded_pattern)
            continue
        if len(matches) > max_results:
            raise ValueError(
                f"-allowed-dirs 模式 {pattern!r} 展开过多 "
                f"({len(matches)} > {max_results}),拒绝配置(防 DoS)"
            )
        expanded.extend(matches)
    return expanded


# mp.Manager() 单例已移到 pool.py(L1 拆分)


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
IDLE_TIMEOUT = 300

# 【生产改造 C4】路径白名单:默认仅允许上传目录 + 系统临时目录
# 部署时务必通过 -allowed-dirs 覆盖为真实业务目录,例如 /data/uploads
ALLOWED_INPUT_DIRS_DEFAULT = ','.join([
    os.path.expanduser('~/uploads'),
    tempfile.gettempdir(),
    "F:\\桌面\\0xi样本\\",
    "/opt/KY/AppsRoot"
])

# 【生产改造 M2】魔法数字提取 — 集中管理便于调优
# 仅 main __main__ 用的常量;handler / security / pool 的常量已下放到各自模块
WORKER_QUEUE_GET_TIMEOUT = 5.0    # worker 进程 task_queue.get 超时(秒)
PREWARN_HEAVY_THRESHOLD = 8       # 预热 worker 数超过此值触发内存/启动时间警告
PREWARM_DEFAULT = 4               # -prewarm 默认值

from http.server import ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor

# 【L1 拆分】Handler / 路由 / 共享状态 / 文件类型 已独立到 handler.py
from handler import (  # noqa: E402,F401
    Handler,
    ROUTES,
    AUDIO_EXTS,
    IMAGE_EXTS,
    pools,
    _ALLOWED_DIRS,
    _ALLOWED_HOSTS,
)
# 【L1 拆分】ElasticProcessPool 已独立到 pool.py
from pool import ElasticProcessPool  # noqa: E402,F401

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


# 【L1 拆分】安全相关 helper 已独立到 security.py(SSRF / 路径白名单 / download_http_file)
from security import (  # noqa: E402,F401  (从 security 透传)
    _is_safe_url,
    _is_safe_path,
    download_http_file,
)



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
    parser.add_argument('-min-workers', type=int, default=PREWARM_DEFAULT, help='空闲超时后最少保留多少 worker(默认 1);生产推荐设为 -prewarm 同值,避免突发流量冷启动')
    parser.add_argument('-asr-min-workers', type=int, default=None, help='ASR 池保活数;指定后覆盖 -min-workers')
    parser.add_argument('-ocr-min-workers', type=int, default=None, help='OCR 池保活数;指定后覆盖 -min-workers')
    parser.add_argument('-max-queue', type=int, default=200, help='单池最大排队任务数(in_flight - alive 的上限),超过直接 503 防 OOM')
    parser.add_argument('-asr-max-queue', type=int, default=None, help='ASR 池队列上限;指定后覆盖 -max-queue')
    parser.add_argument('-ocr-max-queue', type=int, default=None, help='OCR 池队列上限;指定后覆盖 -max-queue')
    parser.add_argument('-idle', type=int, default=IDLE_TIMEOUT)
    # 【生产改造 C4】路径白名单参数
    parser.add_argument('-allowed-dirs', type=str, default=ALLOWED_INPUT_DIRS_DEFAULT,
                        help=('允许的文件路径白名单(逗号分隔绝对路径),展开 ~ 与 $VAR,'
                              '支持 glob 通配符(* ? [..] **);例: ~/uploads,/tmp,~/uploads/2024-*/incoming'))
    # 【生产改造 C1】API Key 认证:任一方式设置即启用,未设置则不强制(开发模式)
    parser.add_argument('-api-key', type=str, default=None,
                        help='API 密钥(启用后客户端必须带 X-API-Key Header,建议用 -api-key-env)')
    parser.add_argument('-api-key-env', type=str, default=None,
                        help='从指定环境变量名读取 API 密钥(避免密钥进 ps)')
    # 【内部主机白名单】允许指定可信内网 host / IP / CIDR,SSRF 校验放过这些
    # 默认空 = 严格 SSRF(仅公网 + hostname 黑名单)。生产内网服务需要显式开
    parser.add_argument('-allowed-internal-hosts', type=str, default='',
                        help=('可信内网主机白名单(逗号分隔),绕过 SSRF 内网检查。\n'
                              '支持 hostname / IP 字面量 / CIDR,例:\n'
                              '  127.0.0.1,localhost\n'
                              '  192.168.1.100,internal.api.local\n'
                              '  10.0.0.0/8,192.168.0.0/16'))
    parser.add_argument('-f', type=str, default=None)
    args = parser.parse_args()

    # 【生产改造 C1】解析最终 API key(命令行 > 环境变量)
    _api_key = args.api_key
    if args.api_key_env:
        _api_key = os.environ.get(args.api_key_env) or _api_key
    # 注入到 Handler(类变量,所有请求共享)
    Handler._api_key = _api_key

    # 【生产改造 C4 + task29】展开白名单目录,支持 ~ $VAR 和 glob 通配符
    # 模式语法:
    #   字面路径  /data/uploads          → 1 个
    #   单层通配  ~/uploads/*           → 匹配 ~/uploads 下所有直接子目录
    #   字符通配  ~/uploads/202?-*/log → 单层内 ? 和 [..]
    #   递归通配  ~/uploads/**          → 匹配所有后代(需显式 ** 才递归)
    #   环境变量  $DATA_ROOT/incoming   → 展开 $DATA_ROOT
    # 安全护栏:展开结果超过 max_results(默认 1000)拒绝配置,防 DoS
    if args.allowed_dirs:
        _ALLOWED_DIRS[:] = [
            os.path.realpath(p)
            for p in _expand_allowed_dirs(args.allowed_dirs, max_results=1000)
        ]
    else:
        _ALLOWED_DIRS.clear()

    # 【生产改造 task39】解析 -allowed-internal-hosts,填 _ALLOWED_HOSTS
    # 默认空 = 严格 SSRF,生产内网场景需显式开
    from security import _parse_trusted_hosts
    parsed = _parse_trusted_hosts(args.allow_internal_hosts)
    _ALLOWED_HOSTS['hostnames'] = parsed['hostnames']
    _ALLOWED_HOSTS['ip_literals'] = parsed['ip_literals']
    _ALLOWED_HOSTS['cidrs'] = parsed['cidrs']
    if parsed['hostnames'] or parsed['ip_literals'] or parsed['cidrs']:
        logger.info("-allowed-internal-hosts 配置: %d hostnames, %d ips, %d cidrs",
                    len(parsed['hostnames']), len(parsed['ip_literals']), len(parsed['cidrs']))

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