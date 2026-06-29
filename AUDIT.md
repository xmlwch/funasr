# FunASR 代码审计报告(详细版)

**审计时间**: 2026-06-26
**审计人**: Claude Code 自动化审计 + 人工复核
**审计范围**: main.py / worker.py / _paths.py / Dockerfile / funasr.spec / requirements.txt / .github/workflows/
**最近 commit**: 737c5d6 (feat: 弹性并发扩容 + per-pool 配置 + /metrics + 3 bug 修复)
**总改动**: +166 / -40 行,2 文件

---

## 总体评估

| 维度 | 评分 | 详细说明 |
|---|---|---|
| 功能正确性 | ⭐⭐⭐⭐ (4/5) | 核心并发逻辑验证过(5 并发 OCR 5/5 成功、扩容触发、min-workers 保活) |
| 安全性 | ⭐⭐ (2/5) | 4 个 Critical 风险全部未防护(认证、SSRF、路径穿越、EOL 镜像) |
| 生产就绪度 | ⭐⭐ (2/5) | 缺认证、缺测试、Dockerfile EOL、容器 root、health check 不真实 |
| 代码质量 | ⭐⭐⭐ (3/5) | 结构清晰,但全 print 无 logging、magic numbers 散落 |
| 可观测性 | ⭐⭐⭐⭐ (4/5) | /metrics + health + 启动横幅都有,但缺请求级 metrics |
| 可维护性 | ⭐⭐ (2/5) | 单文件 700+ 行、无 type hints、注释全中文 |

**结论**: **不建议直接上生产**。Critical 4 项必修,High 5 项尽快修。

---

## 🔴 Critical(4 项)— 上线前必修

### C1. 无任何认证授权 ⚠️⚠️⚠️

**位置**: `main.py:317 Handler` 整个类(共 90 行)
**严重度**: 极高

#### 风险详述

服务监听 `0.0.0.0:5001`(由 `-host` 参数决定,默认 `127.0.0.1`),但只要绑定 `0.0.0.0`(README 文档鼓励)就对外网开放。`Handler.do_POST` 完全没有认证逻辑:

```python
# main.py:320
def do_POST(self):
    model_type = ROUTES.get(self.path)
    if model_type is None:
        self._json(404, ...); return
    target_pool = pools.get(model_type)
    if target_pool is None:
        self._json(503, ...); return
    self._handle_request(model_type, target_pool)  # ← 无认证直接进
```

#### 攻击场景

| 攻击 | 危害 |
|---|---|
| 任意人 POST 大量 OCR 任务 | DoS:模型推理消耗 CPU + 内存,20 个 worker 进程吃满 |
| 重复 POST 同一个文件 | 资源滥用:每次都触发 PaddleOCR 完整流程 |
| 读取服务器任意 .png/.jpg/.wav 文件(配合路径穿越 C4) | 数据泄露 |

#### 修复建议

**方案 A(轻量)**: 加 `-api-key` 参数,Header 校验:

```python
# main.py:444 __main__ argparse 后
parser.add_argument('-api-key', type=str, default=None,
                    help='API 密钥,设置后客户端需带 X-API-Key Header')

# main.py:317 Handler 内
class Handler(BaseHTTPRequestHandler):
    _api_key = None  # 类变量,启动时由 __main__ 注入
    
    def _check_auth(self):
        if not Handler._api_key: return True  # 未设置 = 不校验(开发模式)
        return self.headers.get('X-API-Key') == Handler._api_key
    
    def do_POST(self):
        if not self._check_auth():
            self._json(401, {'code': 401, 'message': 'Unauthorized'}); return
        # ... 原有逻辑

# main.py:__main__ 启动前
Handler._api_key = args.api_key
```

**方案 B(生产级)**: JWT + 用户系统(超出本次审计范围)

**推荐**: 上线前至少用方案 A,几十行代码堵住 90% 滥用风险。

---

### C2. Dockerfile 基于 EOL Debian buster

**位置**: `Dockerfile:1`
**严重度**: 高(安全合规)

#### 风险详述

```dockerfile
FROM python:3.10-slim-buster
RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install ...
```

**事实**:
- Debian buster 于 **2024-06-30** 进入 LTS 阶段,常规安全更新已于 2024-06 终止
- 当前 LTS 支持仅到 2024-08,之后无任何 CVE 修复
- `sed -i 's/deb.debian.org/archive.debian.org/g'` 这个 workaround 标志性地说明镜像已无法正常 update
- `buster-updates` 仓库已删除,所以 `sed '/buster-updates/d'` 强制删掉

**潜在 CVE**: glibc、openssl、libgomp1 等基础库可能存在未修复漏洞。具体数量无法统计(官方已停止索引)。

#### 修复建议

```dockerfile
# 选项 A: 升 bookworm(Debian 12,当前稳定)
FROM python:3.10-slim-bookworm

# 选项 B: 升 bullseye(Debian 11,仍维护)
FROM python:3.10-slim-bullseye
```

**兼容性验证**:
- `libgomp1`、`libgl1-mesa-glx`(替换为 `libgl1`)、`libglib2.0-0`、`libsm6`、`libxext6`、`ffmpeg`、`ccache` 在 bookworm 都有
- `libgl1-mesa-glx` 在 bookworm 已弃用,需改为 `libgl1` + `libglx-mesa0`
- 升级后需重新构建并冒烟测试

**附加**: Dockerfile 是 build-only(产物是独立 PyInstaller 二进制,无运行时容器),所以 USER 指令不适用 — 见下文 H4 修正说明。

---

### C3. SSRF(服务端请求伪造)

**位置**: `main.py:303-315 download_http_file`
**严重度**: 高

#### 风险详述

```python
# main.py:303
def download_http_file(url: str, suffix: str) -> str:
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:
            # ... 直接 GET 任意 URL
```

`urllib.request.urlopen` 默认支持 HTTP/HTTPS/FTP/file,允许重定向,**不对目标地址做限制**。

#### 攻击场景

```bash
# 攻击 1: 云 metadata 服务(获取 IAM 凭证)
curl -X POST http://server:5001/ocr/identify \
  -d '{"filepath": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"}'
# 返回的"图片数据"实际是 AWS IAM credentials,被写入磁盘 temp 文件

# 攻击 2: 内网服务扫描
curl -X POST http://server:5001/ocr/identify \
  -d '{"filepath": "http://192.168.1.1:8080/admin"}'
# 通过返回内容判断端口开放

# 攻击 3: 内网 Redis/Memcached 未授权访问
curl -X POST http://server:5001/ocr/identify \
  -d '{"filepath": "http://10.0.0.5:6379/"}'
```

#### 当前防护

- `Content-Length` 检查:只防超大文件,不防 SSRF
- `MAX_CONTENT_LENGTH = 100MB`:同上
- 无任何 IP 白名单/黑名单

#### 修复建议

新增 `_is_safe_url()`,在 `download_http_file` 入口调用:

```python
# main.py 顶部 import
import ipaddress, socket
from urllib.parse import urlparse

def _is_safe_url(url: str) -> bool:
    """检查 URL 是否指向公网地址,防 SSRF"""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        # 解析所有 IP(防 DNS rebinding)
        try:
            ips = [info[4][0] for info in socket.getaddrinfo(hostname, None)]
        except socket.gaierror:
            return False
        for ip_str in set(ips):
            ip = ipaddress.ip_address(ip_str)
            if (ip.is_private or ip.is_loopback or ip.is_link_local 
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        # 黑名单常见 metadata 地址
        if hostname.lower() in ('metadata.google.internal', 'metadata', 
                                 'kubernetes.default.svc'):
            return False
        return True
    except Exception:
        return False

# main.py:303 download_http_file 修改
def download_http_file(url: str, suffix: str) -> str:
    if not _is_safe_url(url):
        raise ValueError(f"URL not allowed: {url}")
    # ... 原逻辑
```

**注意**: 还需要禁用重定向到内网(`urllib` 默认会跟随 30x),需自定义 opener:

```python
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        raise ValueError(f"Redirect not allowed: {headers.get('Location')}")
opener = urllib.request.build_opener(NoRedirect)
urllib.request.install_opener(opener)
```

---

### C4. 路径穿越(任意文件读取)

**位置**: `main.py:362-365 _handle_request`
**严重度**: 高

#### 风险详述

```python
# main.py:362
real_path = filepath  # ← 用户输入直接用作路径
if os.path.getsize(real_path) > MAX_CONTENT_LENGTH:
    raise ValueError("文件大小超过限制")
text = target_pool.submit(service_type, real_path)
```

#### 攻击场景

OCR 端点验证 `.png/.jpg` 等图片后缀,但**只检查扩展名,不验证实际路径**:

```bash
# 攻击 1: 通过 URL 下载 SSRF(配合 C3)
# 即使服务部署在内网,攻击者可通过 URL 让服务去读内网任意图片
# 之后通过结果推断文件存在性(空结果 = 路径不存在但服务可达)

# 攻击 2: 直接读敏感文件(.wav 端点可读任意文件)
curl -X POST http://server:5001/funasr/identify \
  -d '{"filepath": "/etc/shadow.wav"}'  # 后缀.wav 满足验证
# 实际上传 /etc/shadow 给 ASR 模型
# ASR 会"识别"成功,返回 garbage text,但文件已被读取并可能缓存

# 攻击 3: 读 SSH 密钥
curl -X POST http://server:5001/ocr/identify \
  -d '{"filepath": "/root/.ssh/id_rsa.png"}'
```

#### 当前防护

- 后缀白名单:`AUDIO_EXTS` / `IMAGE_EXTS` — **只防类型,不防路径**
- 文件大小检查:无关
- 路径规范化:无

#### 修复建议

```python
# main.py 顶部(可配置)
ALLOWED_INPUT_DIRS = [
    os.path.expanduser('~'),
    tempfile.gettempdir(),
    # 生产环境应改为: ['/data/uploads', '/var/funasr/incoming']
]

def _is_safe_path(filepath: str) -> str:
    """规范化并校验路径必须在白名单目录内,返回绝对路径"""
    real_path = os.path.realpath(filepath)  # 解析 .. 和符号链接
    for allowed in ALLOWED_INPUT_DIRS:
        allowed_abs = os.path.realpath(allowed)
        if real_path == allowed_abs or real_path.startswith(allowed_abs + os.sep):
            return real_path
    raise ValueError(f"Path not allowed (不在白名单目录): {filepath}")

# main.py:362 替换
real_path = _is_safe_path(filepath)
if os.path.getsize(real_path) > MAX_CONTENT_LENGTH:
    raise ValueError("文件大小超过限制")
```

**生产配置**: 通过环境变量或命令行参数指定白名单,避免硬编码:

```python
parser.add_argument('-allowed-dirs', type=str, default='~/uploads,/tmp',
                    help='允许的文件路径前缀,逗号分隔')
```

---

## 🟠 High(5 项)— 尽快修

### H1. 健康检查不真实(假阳性)

**位置**: `main.py:382-383 do_GET`

#### 风险详述

```python
def do_GET(self):
    if self.path in ['/funasr/health', '/ocr/health']:
        self._json(200, {'code': 200, 'status': 'ok'})  # ← 永远 200
```

**问题**:
- 服务启动但模型还在加载时,健康检查返回 200
- 所有 worker 进程崩溃后,健康检查仍返回 200
- K8s liveness probe 会认为服务正常,**不会触发重启**

#### 修复建议

```python
def do_GET(self):
    if self.path in ['/funasr/health', '/ocr/health']:
        model = 'asr' if 'funasr' in self.path else 'ocr'
        pool = pools.get(model)
        if pool is None:
            self._json(503, {'code': 503, 'status': 'pool_not_initialized'}); return
        s = pool.stats()
        # 至少有一个 idle worker 才算健康
        if s['alive'] == 0 or s['alive'] == s['dead']:
            self._json(503, {'code': 503, 'status': 'no_alive_workers',
                             'stats': s}); return
        self._json(200, {'code': 200, 'status': 'ok', 'stats': s})
```

**附加**: K8s 配 readiness probe 用同样的端点,liveness 用 `/livez`(永远 200,只检查进程存活)。

---

### H2. 超时任务残留内存泄漏

**位置**: `main.py:191-209 submit` 轮询逻辑

#### 风险详述

```python
# main.py:191
try:
    start_time = time.time()
    while True:
        if self.is_shutting_down:
            raise RuntimeError("服务正在关闭，推理被中断")
        if task_id in self.results:
            data = self.results.pop(task_id)
            if isinstance(data, Exception): raise data
            return data
        if time.time() - start_time > INFERENCE_TIMEOUT:
            raise TimeoutError("推理超时")  # ← 抛了但 results 里的条目还在!
```

**后果**:
- worker 进程慢于 300s 超时(模型卡死、IO 阻塞)
- submit 抛 TimeoutError,调用方拿到 408
- 但 worker 后续若完成,会 `results[task_id] = res` — **这条永远留在 results 里**
- `results` 是 Manager.dict(跨进程代理),无法 GC,内存只增不减
- 高 QPS + 持续超时 → OOM

#### 修复建议

```python
finally:
    # 不管成功/超时/关闭,都清理 results,worker 写不进来也无所谓
    self.results.pop(task_id, None)
    with self.lock:
        self.in_flight -= 1
```

`pop(task_id, None)` 是安全的,即使 worker 没写也不报错。

---

### H3. 无任何单元测试

**事实**:
- `find . -name "test_*.py"` 无结果
- `.github/workflows/build.yml` 只有 build 步骤,无 test
- `pyproject.toml` / `pytest.ini` / `setup.cfg` 都不存在

#### 风险详述

- 重构无保护网,改一行可能引入回归
- 之前的 3 个 bug(in_flight 双递减、stats race、min>max 无校验)都是开发时漏掉的
- 多人协作无回归保障

#### 修复建议

最小测试集:

```
tests/
├── test_elastic_pool.py      # 池核心逻辑
│   ├── test_in_flight_increment_decrement
│   ├── test_scale_up_trigger
│   ├── test_min_workers_floor
│   ├── test_queue_full_rejection
│   └── test_shutdown_drains_workers
├── test_handler.py           # HTTP 层
│   ├── test_health_returns_503_when_no_workers
│   ├── test_metrics_format
│   └── test_post_404_unknown_route
├── test_paths.py             # 路径工具
│   ├── test_get_pkg_dir_frozen_vs_source
│   └── test_setup_bundled_env_idempotent
└── conftest.py
```

CI 增加:

```yaml
- name: Run tests
  run: |
    pip install pytest
    pytest tests/ -v --tb=short
```

---

### ~~H4. 容器内以 root 运行~~ (已剔除)

**结论**: **不适用**,已从审计中剔除。

#### 原因

`Dockerfile` 在本仓库中**只用于打包二进制**,不用于运行服务。流程见 `.github/workflows/build.yml`:

```yaml
- name: Build inside Docker (glibc 2.28)
  run: |
    docker build --platform linux/${{ matrix.arch }} -t funasr-builder .
    docker create --name funasr-tmp funasr-builder
    docker cp funasr-tmp:/build/dist/funasr ./funasr-${{ matrix.name }}  # ← 提取二进制
    docker rm funasr-tmp  # ← 容器用完即删
```

PyInstaller 把 Python 解释器和所有依赖打进 `funasr-linux-x86_64`,**自带独立运行时**。最终用户在裸机或自有容器中执行二进制,跟 build 容器无关。

#### 影响

- CIS Docker Benchmark 类建议(USER 指令、HEALTHCHECK、read-only fs)对 build-only 容器无意义
- 容器寿命短(几分钟),暴露面可忽略
- 如果未来改用 build+run 一体化镜像(类似 `FROM python:slim` + `COPY dist/`),H4 才重新适用

---

### H5. 500 错误信息直接返回客户端

**位置**: `main.py:374-377 _handle_request`

#### 风险详述

```python
except Exception as e:
    status_code = 503 if "正在关闭" in str(e) else 500
    self._json(status_code, {'code': status_code, 'message': str(e) or '系统繁忙', 'data': None})
```

`str(e)` 可能包含:
- 完整文件路径(如 `FileNotFoundError: [Errno 2] No such file or directory: '/etc/passwd.png'`)
- Python 库版本(如 `paddleocr 2.9.1 error: ...`)
- 内部状态(模型路径、worker PID)
- 堆栈敏感信息

#### 修复建议

```python
import logging
logger = logging.getLogger('funasr.handler')

except Exception as e:
    if "正在关闭" in str(e):
        self._json(503, {'code': 503, 'message': '服务正在关闭', 'data': None})
    else:
        # 详细错误入日志(运维看),客户端只收到通用消息
        logger.exception("unhandled error in _handle_request")
        self._json(500, {'code': 500, 'message': '内部错误,请稍后重试', 'data': None})
```

---

## 🟡 Medium(8 项)— 建议修

### M1. 全 print 无结构化 logging

**位置**: 全代码(`main.py` 50+ 处 print,`worker.py` 10+ 处)

**问题**: 生产环境:
- 无法区分 INFO/WARN/ERROR
- 无时间戳(用 print 默认无)
- 无法路由到 ELK/Loki
- 多个 worker 同时输出时混淆

**修复**:
```python
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
logger = logging.getLogger('funasr')

# 替换 print 为 logger.info/debug/warning/error
```

worker 进程需要单独 logger 名:`funasr.worker.{pid}`。

---

### M2. 魔法数字散落

**位置**: 多处

| 常量 | 当前值 | 位置 | 建议名 |
|---|---|---|---|
| worker 队列 get 超时 | `5.0` | `worker.py:112` | `WORKER_QUEUE_GET_TIMEOUT` |
| 监控线程间隔 | `10` | `main.py:260` | `MONITOR_INTERVAL` |
| submit 轮询间隔 | `0.05` | `main.py:206` | `POLL_INTERVAL` |
| HTTP 请求队列 | `128` | `main.py:318` | `HTTP_REQUEST_QUEUE_SIZE` |
| 启动预热 wait_ready 超时 | `60` | `main.py:156` | `WAIT_READY_TIMEOUT` |

**修复**: 顶部统一声明:

```python
WORKER_QUEUE_GET_TIMEOUT = 5.0
MONITOR_INTERVAL = 10
POLL_INTERVAL = 0.05
HTTP_REQUEST_QUEUE_SIZE = 128
WAIT_READY_TIMEOUT = 60
```

---

### M3. `_paths.py` 注释与实现不一致

**位置**: `_paths.py:28-44`

```python
def setup_bundled_env():
    """frozen 时把 _MEIPASS/bin 注入 PATH,把 _MEIPASS/lib 注入 LD_LIBRARY_PATH,
    并设 TORCHAUDIO_USE_FFMPEG_PATH 等环境变量..."""
    # 实际只设置 PATH,没设 LD_LIBRARY_PATH 也没设 TORCHAUDIO_USE_FFMPEG_PATH
    if not getattr(sys, 'frozen', False):
        return
    pkg = get_pkg_dir()
    bin_dir = os.path.join(pkg, 'bin')
    lib_dir = os.path.join(pkg, 'lib')  # ← 算了不用

    if os.path.isdir(bin_dir):
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ.get('PATH', '')
```

**修复**:
- 选项 A: 删 `lib_dir` 死代码,更新 docstring
- 选项 B: 实现承诺的功能(LD_LIBRARY_PATH、TORCHAUDIO_USE_FFMPEG_PATH)

---

### M4. `start_worker()` 在锁内 spawn 进程

**位置**: `main.py:189`

```python
with self.lock:
    if alive < self.max_workers and self.in_flight >= alive:
        self.start_worker()  # ← 在锁内,其他 submit 阻塞 ~10ms
```

**影响**: 单次 ~10ms 可接受,但突发 100 并发时所有 submit 串行等锁。

**修复**: 把 `start_worker()` 移出锁:

```python
need_scale = False
with self.lock:
    self.in_flight += 1
    alive = sum(...)
    if self.in_flight > alive + self.max_queue:
        self.in_flight -= 1
        raise RuntimeError(...)
    if alive < self.max_workers and self.in_flight >= alive:
        need_scale = True  # 仅标记
        self.scale_events += 1
    self.task_queue.put(...)

if need_scale:
    self.start_worker()  # 锁外 spawn
```

---

### M5. `stats()` 跨进程读 Manager.dict

**位置**: `main.py:213-238`

**问题**: `/metrics` 每次调用都跨进程读 Manager.dict(8-9 个字段),Prometheus 15s scrape 没事,如果高频(1s)或 scraper 多并发就成瓶颈。

**修复**: 本地缓存 + 定时刷新:

```python
class ElasticProcessPool:
    def __init__(self, ...):
        ...
        self._cached_stats = {}
        self._stats_cache_time = 0
        self._stats_cache_ttl = 1.0  # 1s 缓存
    
    def stats(self):
        now = time.time()
        if now - self._stats_cache_time < self._stats_cache_ttl:
            return self._cached_stats
        # ... 真实计算
        self._cached_stats = result
        self._stats_cache_time = now
        return result
```

---

### M6. PaddleOCR monkey patch 每次 worker 都重写

**位置**: `worker.py:36-41`

```python
if sys.platform == 'win32':
    import paddle.inference
    original_switch_ir_optim = paddle.inference.Config.switch_ir_optim
    def fake_switch_ir_optim(self, enable):
        return original_switch_ir_optim(self, False)
    paddle.inference.Config.switch_ir_optim = fake_switch_ir_optim
```

20 个 worker 各自 patch 全局,最后一个赢。无害但不优雅。

**修复**: 在 `main.py` 启动时一次性 patch(主进程 + worker 都会执行模块顶层代码,但 paddle.inference 只 import 一次):

```python
# main.py 顶部(在 from worker 之前)
if sys.platform == 'win32':
    import paddle.inference
    _orig = paddle.inference.Config.switch_ir_optim
    paddle.inference.Config.switch_ir_optim = lambda self, e: _orig(self, False)
```

worker.py 删掉这段。

---

### M7. 预热串行 spawn

**位置**: `main.py:518-519`

```python
for name, cfg in pool_cfg.items():
    pool = pools[name]
    for _ in range(cfg['prewarm']):
        pool.start_worker()  # 串行
```

`prewarm=20` 串行要 1s+。

**修复**: 用 ThreadPoolExecutor 并行 spawn:

```python
def _spawn(pool):
    pool.start_worker()

with ThreadPoolExecutor(max_workers=total_prewarm) as ex:
    futures = []
    for name, cfg in pool_cfg.items():
        pool = pools[name]
        for _ in range(cfg['prewarm']):
            futures.append(ex.submit(_spawn, pool))
    for f in futures:
        f.result()
```

---

### M8. `requirements.txt` 未全 pin

```text
funasr-onnx
funasr
onnxruntime
paddlepaddle==3.3.1
paddleocr==2.9.1
more_itertools
```

**问题**: `funasr-onnx`、`funasr`、`onnxruntime`、`more_itertools` 没 pin,可能因上游版本变化导致 API 不兼容。

**修复**: 用 `pip freeze` 生成精确版本:

```text
funasr-onnx==1.1.13
funasr==1.2.7
onnxruntime==1.17.1
paddlepaddle==3.3.1
paddleocr==2.9.1
more_itertools==10.3.0
```

---

## 🟢 Low(8 项)— Nitpick

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| L1 | `main.py` 整体 | 单文件 700+ 行,职责混在一起 | 拆 `pool.py` / `handler.py` / `cli.py` / `paths.py` |
| L2 | 全代码 | 无 type hints,IDE 提示弱 | 逐步加 `def foo(x: int) -> str:` |
| L3 | 注释 | 全部中文,国际协作者不易参与 | 接受现状,新代码双语注释 |
| L4 | 装饰输出 | `print("=" * 60)` 横幅 | 改用 logging,启动横幅用单独函数 |
| L5 | `import traceback` | `main.py:3` 导入但从未使用 | 删 |
| L6 | 模块级 `pools: dict = {}` | 全局可变状态,难测试 | 改成类封装或依赖注入 |
| L7 | `pools.update({...})` | 服务启动后改不动 | 接受,这是设计选择 |
| L8 | `from worker import elastic_worker_loop` | 顶层导入耦合 | 可接受 |

---

## 📊 风险热力图

```
        容易利用    需要内网     极难
高危     C1 认证     C3 SSRF    C4 路径穿越
        H1 健康      H2 泄漏     
中危     H5 错误     C2 EOL      M6 monkey
        M1 logging  M2 magic    M3 docstring
低危     L5 import   L1 单文件   L4 装饰输出
```

**C 类 4 项必修**,其他按业务紧迫度排。

---

## 🎯 推荐修复路线

### 第一批(上线前,半天内,~50 行)

| 项 | 改动量 | 风险 |
|---|---|---|
| C1 加 API Key | ~15 行 | 需协调客户端带 Header |
| C2 Dockerfile 升 bookworm | ~5 行 | 需重新构建镜像 |
| C3 SSRF 防护 | ~25 行 | 低,加白名单 |
| C4 路径白名单 | ~15 行 | 低,加 `realpath` 校验 |
| H1 健康检查真实化 | ~10 行 | 低 |

### 第二批(1 周内,~30 行)

| 项 | 改动量 | 风险 |
|---|---|---|
| H2 超时清理 results | ~3 行 | 极低 |
| H5 错误信息脱敏 | ~10 行 | 低 |

### 第三批(持续,~200 行)

| 项 | 改动量 | 风险 |
|---|---|---|
| H3 单元测试 | ~200 行 | 低 |
| M1-M8 | ~50 行 | 低 |

---

## ✅ 已修复(本 commit 内)

| Bug | 引入版本 | 修复 commit |
|---|---|---|
| in_flight 双递减 | 737c5d6(同 commit 内) | 737c5d6 |
| stats() 跨进程 race | 737c5d6(同 commit 内) | 737c5d6 |
| min_workers > max_workers 无校验 | 737c5d6(同 commit 内) | 737c5d6 |
| 5 并发 OCR 只过 1 个 | 历史 | 737c5d6 |
| 池空闲缩到 1 个 | 历史 | 737c5d6 |
| Windows GBK ✓ 报错 | 历史 | 737c5d6 |

---

## 📚 参考资料

- OWASP SSRF: https://owasp.org/www-community/attacks/Server_Side_Request_Forgery
- CIS Docker Benchmark: https://www.cisecurity.org/benchmark/docker
- Python multiprocessing best practices: https://docs.python.org/3/library/multiprocessing.html
- Debian release cycle: https://wiki.debian.org/DebianReleases

---

*报告生成: Claude Code 自动化审计,详细修复代码见对话历史*


---

# 附录:构建链审计(2026-06-29 增量)

## 审计范围

`Dockerfile` / `.github/workflows/build.yml` / `funasr.spec` / `requirements.txt`

## 🔴 发现与修复

| # | 严重度 | 问题 | 修复 | commit |
|---|---|---|---|---|
| BC-1 | 🔴 | `.github/workflows/build.yml:41` `runs-on: ${{ matrix.runs-on }` 漏右 `}}` — linux job 全失败 | 补 `}}` | 本次 |
| BC-2 | 🔴 | Dockerfile 没 COPY 新拆分的 `pool.py / security.py / handler.py` — L1 拆分后 PyInstaller 会找不到模块 | 加 COPY | 本次 |
| BC-3 | 🟠 | funasr.spec `hiddenimports` 没列新模块,运行时 ModuleNotFoundError | 加 `'pool' / 'security' / 'handler'` | 本次 |
| BC-4 | 🟡 | requirements.txt 仍用 `>=` 不严谨 | 改为 `==` 精确 pin(实测版本) | 本次 |

## Dockerfile 改进

- ✅ EOL:buster → bookworm(已完成 commit `c2033c7`)
- ✅ 依赖调整:`libgl1-mesa-glx` 拆分 → `libgl1 + libglx-mesa0`
- ✅ 加 OCI LABELS(`org.opencontainers.image.*`)
- ✅ COPY 全部分拆的 Python 模块(pool.py / security.py / handler.py)
- ✅ `pyinstaller` 分独立 RUN,源码改动不重装 torch/paddle(利用层缓存)
- 🟡 待办:Dockerfile 内加 HEALTHCHECK(无意义,因为 build-only 不运行服务)

## CI 改进(`.github/workflows/build.yml`)

- ✅ 修语法错误(漏 `}`)
- ✅ test job:`push` 到 main / PR 都跑 pytest
- ✅ linux 矩阵:amd64 + arm64
- ✅ windows 单独 job
- ✅ release job:tag 触发,合并 3 个平台产物
- 🟡 待办:加 cache(`actions/cache@v4`)缓存 pip 依赖,加速 build

## funasr.spec 改进

- ✅ `pathex=[SPEC_DIR]` 让 PyInstaller 能找 worker.py / pool.py / handler.py / security.py
- ✅ `hiddenimports` 列全 4 个拆出模块
- ✅ runtime_hooks=[](用 `mp.freeze_support()` 替代)
- ✅ excludes:测试模块(torch.tests / paddleOCR.tests)

## requirements.txt pin 策略

```text
funasr-onnx==0.4.1     # 实测 2026-06-29
funasr==1.3.1           # 实测 2026-06-29
onnxruntime==1.23.2     # 实测 2026-06-29
paddlepaddle==3.3.1     # 锁定(README 指定)
paddleocr==2.9.1       # 锁定(README 指定)
more_itertools>=10.3.0  # 宽松(paddleocr 间接依赖,小版本兼容)
```

**升级流程**:改这里 → 重跑 5 并发 OCR 测试 → 无回归再 commit。

## Low 类(L)残留项 — 工厂改造 backlog

| # | 项 | 状态 |
|---|---|---|
| L2 | type hints 全代码 | 🔴 TODO(代码量 ~300 行) |
| L5 | 死 import 清理 | 🟢 部分完成(import traceback / queue / uuid 已删) |
| L6 | `pools` 字典封装为类 | 🔴 TODO(改为 AppState 单例或 AppContext dataclass) |
| L7 | handler 拆分 read/write 方法 | 🟢 不需要(当前方法长度合理) |
| L8 | README 国际化 | 🟢 不需要(项目中文优先) |

## 已知风险(非本次范围)

| 风险 | 说明 | 处理建议 |
|---|---|---|
| shm.dll WinError 127 | Windows Anaconda 环境缺 VC++ Runtime | 用户机器装 VC++ Redistributable 2015-2022 |
| glibc 版本 | Linux 二进制需 glibc ≥ 2.28 | README 注明,CI 在 ubuntu 22.04/24.04 build |
| paddle 编译期单线程 | `FLAGS_*_THREADS=1` | 打包时设置,无生产影响 |
