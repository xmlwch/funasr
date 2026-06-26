# FunASR 中高风险修复方案

## Context(背景)

**为什么做这次修复**: FunASR 服务最近完成并发扩容功能(commit 737c5d6)后,代码审计(`AUDIT.md`)发现多个生产级风险:无认证导致任意调用、SSRF 可访问云 metadata、路径穿越可读服务器任意文件、Dockerfile 基于已 EOL 的 Debian buster、健康检查假阳性、错误信息泄漏内部细节等。**当前不建议直接上生产**。

**预期结果**: 完成 Critical 4 项 + High 4 项 + Medium 8 项修复后,服务达到生产部署标准,引入结构化 logging 与单元测试网,代码可维护性提升。

**修复范围**: C1-C4 + H1/H2/H3/H5 + M1-M8(共 16 项,Medium 以下不动)

---

## 总体策略

按 **风险等级 + 实施独立性** 分 4 批:

| 批 | 范围 | 工时 | 何时上 |
|---|---|---|---|
| 1 | C1-C4(Critical) | 半天 | 上线前必修 |
| 2 | H1/H2/H5(High,无 H3 测试) | 1-2 天 | 上线后尽快 |
| 3 | H3 测试 + M1-M8(Medium) | 1 周 | 持续 |
| 4 | L 类(Low nitpick) | 持续 | 不定 |

**核心原则**:
- 每批独立 PR、独立回滚
- 默认值保留旧行为,新参数 opt-in
- 不引入手动跑通的临时脚本
- 用 `logging` 替代 `print`(worker 进程用 `funasr.worker.{pid}` 命名)

---

## 第一批:Critical 上线前必修(~60 行)

### C1 API Key 认证

**文件**: `main.py`

**改动**:
- `main.py` argparse 加 `-api-key`(密钥直接传)和 `-api-key-env`(从环境变量读,避免 ps 暴露)
- `Handler` 类顶部加 `_api_key: Optional[str] = None`
- 加方法 `_check_auth(self) -> bool`,用 `hmac.compare_digest()` 防时序攻击
- `do_POST` 入口先调 `_check_auth()`,失败返 401 + `WWW-Authenticate: X-API-Key` 头
- `do_GET` 的 `/metrics` 也走认证(防止暴露池容量给侦察)
- `__main__` 启动前注入 `Handler._api_key`
- `-f` CLI 模式(本地调用)用 `Request` 对象附加 `X-API-Key` Header

**关键代码**:
```python
parser.add_argument('-api-key', type=str, default=None,
                    help='API 密钥(启用后客户端必须带 X-API-Key Header)')
parser.add_argument('-api-key-env', type=str, default=None,
                    help='从指定环境变量名读取 API 密钥(避免密钥进 ps)')

class Handler:
    _api_key = None
    def _check_auth(self) -> bool:
        if not Handler._api_key: return True  # 开发模式不强制
        return hmac.compare_digest(
            self.headers.get('X-API-Key', ''), Handler._api_key)
```

### C2 Dockerfile EOL 升级

**文件**: `Dockerfile`

**改动**:
- 第 1 行 `FROM python:3.10-slim-buster` → `FROM python:3.10-slim-bookworm`
- 删 `sed -i 's/deb.debian.org/archive.debian.org/g'` 和 `sed -i '/buster-updates/d'` 两个 workaround
- 第 7 行依赖调整:`libgl1-mesa-glx` 拆成 `libgl1` + `libglx-mesa0`(bookworm 已移除 mesa-glx)
- 加 `apt-get clean` 双保险

**关键代码**:
```dockerfile
FROM python:3.10-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    binutils libgomp1 libgl1 libglx-mesa0 libglib2.0-0 \
    libsm6 libxext6 ffmpeg ccache \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean
```

**注意**: Dockerfile 只用于 build,产物是 PyInstaller 独立二进制,运行容器不存在。**用户已确认 H4 USER 指令不适用**。

### C3 SSRF 防护

**文件**: `main.py`

**改动**:
- 顶部 import `ipaddress`、`socket`、`from urllib.parse import urlparse`
- 加 `_is_safe_url()` 函数:`getaddrinfo` 解析所有 IP,**任何一个**是 private/loopback/link-local/reserved/multicast/unspecified 就拒
- 加 `_NoRedirect` 类(继承 `HTTPRedirectHandler`),拦截 301/302/303/307/308 重定向(防 30x 跳到内网绕开)
- `download_http_file` 入口调 `_is_safe_url()`,urlopen 替换为 `build_opener(_NoRedirect).open()`
- 常见 metadata 主机名黑名单:`metadata.google.internal`、 `metadata`、 `kubernetes.default.svc`

**关键代码**:
```python
import ipaddress, socket
from urllib.parse import urlparse

def _is_safe_url(url: str) -> bool:
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'): return False
        host = p.hostname
        if not host: return False
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
    def http_error_301(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_302(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_303(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_307(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_308(self, req, fp, code, msg, headers): self._block(headers)
    def _block(self, headers):
        raise ValueError(f"HTTP redirect not allowed: {headers.get('Location')}")
```

### C4 路径白名单

**文件**: `main.py`

**改动**:
- 顶部加常量 `ALLOWED_INPUT_DIRS_DEFAULT = ','.join([os.path.expanduser('~/uploads'), tempfile.gettempdir()])`
- argparse 加 `-allowed-dirs`(逗号分隔绝对路径,展开 ~ 和环境变量)
- 加 `_is_safe_path()` 函数,用 `os.path.realpath()` 解析符号链接和 `..`,校验必须在白名单目录内
- `args.allowed_dirs_list` 在启动前展开
- `_handle_request` 第 362 行 `real_path = filepath` → `real_path = _is_safe_path(filepath)`
- URL 下载路径(`tmp_path`)不需白名单校验(`tempfile.mkstemp` 已在 temp 目录)

**关键代码**:
```python
parser.add_argument('-allowed-dirs', type=str, default=ALLOWED_INPUT_DIRS_DEFAULT,
                    help='允许的文件路径白名单(逗号分隔绝对路径)')

def _is_safe_path(filepath: str) -> str:
    real = os.path.realpath(filepath)
    for allowed in args.allowed_dirs_list:
        if real == allowed or real.startswith(allowed + os.sep):
            return real
    raise ValueError(f"Path not allowed: {filepath}")

# 启动前
args.allowed_dirs_list = [
    os.path.realpath(os.path.expanduser(os.path.expandvars(d.strip())))
    for d in args.allowed_dirs.split(',') if d.strip()
]
```

### 批 1 验证

| 测试 | 期望 |
|---|---|
| 不设 `-api-key` POST | 200 |
| 设 `-api-key secret123` 不带 Header | 401 |
| 设 `-api-key secret123` 带正确 Header | 200 |
| URL `http://169.254.169.254/...` | 400, `URL not allowed` |
| URL `http://192.168.1.1/admin.png` | 400, `URL not allowed` |
| URL 重定向到内网(httpbin.org/redirect-to) | 400, `HTTP redirect not allowed` |
| `{"filepath": "/etc/passwd.png"}` | 400, `Path not allowed` |
| `{"filepath": "~/uploads/test.png"}`(test.png 在该目录) | 200 |
| 符号链接 `~/uploads/sneaky.png → /etc/passwd` | 400,realpath 解析后拒 |
| `/metrics` 无 key | 401 |
| `/metrics` 有 key | 200,Prometheus 格式 |

---

## 第二批:High 尽快修(~30 行)

### H1 健康检查真实化

**文件**: `main.py`

**改动**:
- `do_GET` health 分支检查 pool.idle >= 1 才 200,否则 503 + 返回 stats
- 新增 `/livez` 端点(永远 200,用于 K8s liveness,避免重启风暴)
- `/funasr/health`、`/ocr/health` 用于 K8s readiness probe,真实反映"能否接活"

**关键代码**:
```python
def do_GET(self):
    if self.path in ['/funasr/health', '/ocr/health']:
        model = 'asr' if 'funasr' in self.path else 'ocr'
        pool = pools.get(model)
        if pool is None or pool.stats()['idle'] == 0:
            self._json(503, {'code': 503, 'status': 'not_ready'}); return
        self._json(200, {'code': 200, 'status': 'ok', 'stats': pool.stats()})
    elif self.path == '/livez':
        self._json(200, {'code': 200, 'status': 'alive'})
    elif self.path == '/metrics':
        # ... 原逻辑
```

### H2 超时清理 results

**文件**: `main.py`

**改动**:
- `submit` 的 `finally` 块加 `self.results.pop(task_id, None)`,worker 即使晚到写入也无所谓(下次 submit 同时清理)

**关键代码**:
```python
finally:
    # 不管成功/超时/关闭/队列满,都清理 results
    self.results.pop(task_id, None)
    with self.lock:
        self.in_flight -= 1
```

### H5 500 错误信息脱敏

**文件**: `main.py`

**改动**:
- 顶部 `import logging` + `logger = logging.getLogger('funasr.handler')`
- `except Exception as e` 用 `logger.exception()` 记详细 traceback,客户端只收通用消息
- `except (ValueError, FileNotFoundError)` 保留 `str(e)`(校验类错误对用户调试有用,已被 `_is_safe_path` 过滤过)

**关键代码**:
```python
import logging
logger = logging.getLogger('funasr.handler')

except Exception as e:
    if "正在关闭" in str(e):
        self._json(503, {'code': 503, 'message': '服务正在关闭', 'data': None})
    else:
        logger.exception("unhandled error in _handle_request (path=%s)", self.path)
        self._json(500, {'code': 500, 'message': '内部错误,请稍后重试', 'data': None})
```

### 批 2 验证

| 测试 | 期望 |
|---|---|
| 服务刚启动 worker 还在加载,`/funasr/health` | 503 |
| wait_ready 完成后 `/funasr/health` | 200 |
| `kill -9` 杀掉所有 worker 后 `/funasr/health` | 503 |
| `/livez` 任何时候 | 200 |
| 模拟 worker 抛异常,客户端响应 | 500 + 通用消息(无 Traceback) |
| `pool.results` dict 持续超时场景下大小 | 稳定(不增长) |

---

## 第三批:High 测试 + Medium 改进(~200 行)

### H3 单元测试套件

**新建文件**:
- `tests/conftest.py`:fixture(tmp 目录、mock pool、清理全局 pools)
- `tests/test_elastic_pool.py`:in_flight 增减、扩容触发、min_workers 保活、队列满拒收、shutdown drain、timeout 清理 results
- `tests/test_handler.py`:health 503/200 切换、metrics 格式、404 未知路由、500 脱敏、401 auth、SSRF 拦截、路径拦截
- `tests/test_paths.py`:frozen vs source 模式、setup_bundled_env 幂等
- `pytest.ini`:配置 testpaths、markers(frozen 标记需要 PyInstaller build)
- `requirements-dev.txt`:pytest、pytest-mock、pytest-cov

**关键 mock 技巧**: 多进程池难测,用 `monkeypatch.setattr(mp, 'Process', FakeProcess)` 替成单线程 fake,Manager.dict 用普通 dict 替身。

**CI 改动**: `.github/workflows/build.yml` 新增 test job,`push` 触发(不只 tag):
```yaml
- name: Run tests
  run: pip install -r requirements-dev.txt && pytest tests/ -v --tb=short
```

### M1 结构化 logging

**文件**: `main.py` / `worker.py`

**改动**:
- `main.py` 顶部 `logging.basicConfig()`,格式带时间戳 + level + name
- 50+ 处 `print` 替换为 `logger.info/warning/error`
- `-f` 模式的 `print('错误: ...', file=sys.stderr)` 保留(给最终用户 CLI 输出)
- `worker.py` 用 `logging.getLogger(f'funasr.worker.{os.getpid()}')`
- 通过 `FUNASR_LOG_LEVEL` 环境变量控制级别

### M2 魔法数字提取

**文件**: `main.py` / `worker.py`

**改动**:
- `main.py` 常量区追加:`MONITOR_INTERVAL=10`、`POLL_INTERVAL=0.05`、`HTTP_REQUEST_QUEUE_SIZE=128`、`WAIT_READY_TIMEOUT=60`、`STATS_CACHE_TTL=1.0`、`PREWARN_HEAVY_THRESHOLD=8`
- `worker.py` 顶部加 `WORKER_QUEUE_GET_TIMEOUT=5.0`
- 所有 `time.sleep(10)` / `time.sleep(0.05)` 等替换为常量引用

### M3 `_paths.py` 清理

**文件**: `_paths.py`

**改动**:
- 删死代码 `lib_dir = os.path.join(pkg, 'lib')`
- 更新 docstring 反映真实行为(只注入 PATH,故意不注 LD_LIBRARY_PATH)
- 函数体保留核心逻辑

### M4 锁外 spawn

**文件**: `main.py`

**改动**:
- `submit()` 用 `need_scale` 标记替代直接调 `start_worker()`,把 spawn 移到 `with self.lock` 之外
- 锁内只做:计数 + 检查 + put task

**关键代码**:
```python
need_scale = False
with self.lock:
    self.in_flight += 1
    alive = sum(...)
    if self.in_flight > alive + self.max_queue:
        self.in_flight -= 1
        raise RuntimeError("队列已满")
    if alive < self.max_workers and self.in_flight >= alive:
        need_scale = True
        self.scale_events += 1
    self.task_queue.put({...})
if need_scale:
    self.start_worker()  # 锁外 spawn
```

### M5 stats 缓存

**文件**: `main.py`

**改动**:
- `ElasticProcessPool.__init__` 加 `_stats_cache`、`_stats_cache_time`、`_stats_cache_ttl = STATS_CACHE_TTL`
- `stats()` 入口检查 TTL,未过期返回缓存,否则重算并更新缓存

### M6 PaddleOCR monkey patch 提升

**文件**: `worker.py`

**改动**:
- 从 `init_worker_processes`(每次模型加载都 patch)提到 `worker.py` 模块顶层(每个 worker 进程只 patch 一次)
- **不能移到 main.py**:spawn 子进程不继承父进程的 Python 对象 patch
- 加 `try/except AttributeError` 兜底(paddle 升级可能移除 `switch_ir_optim` 方法)

### M7 预热并行 spawn

**文件**: `main.py`

**改动**:
- 用 `ThreadPoolExecutor(max_workers=total_prewarm)` 并行 spawn
- `start_worker()` 本身在 Windows 上 `CreateProcess` 是同步但很快,~100ms 完成 20 个并发

### M8 requirements 全 pin

**文件**: `requirements.txt`

**改动**:
- 用 `pip freeze` 取真实版本号,替换 `funasr-onnx`、`funasr`、`onnxruntime`、`more_itertools` 的非 pin 行
- 已有 pin 的 `paddlepaddle==3.3.1`、`paddleocr==2.9.1` 保留

### 批 3 验证

| 测试 | 期望 |
|---|---|
| `pytest tests/ -v` | 全绿 |
| `pytest --cov=main --cov=worker` | 覆盖率 ≥ 50% |
| `FUNASR_LOG_LEVEL=DEBUG ./funasr.exe` | DEBUG 日志输出 |
| worker 进程日志 | 带 PID(如 `funasr.worker.12345: ✓ ASR 模型加载完成`) |
| `time.time()` 包住 prewarm=20 | 串行 ~1s → 并行 ~100ms |
| `/metrics` 100 并发请求 CPU | 不升高(1s 缓存生效) |
| 跑 5 并发 OCR P50/P99 | 与 737c5d6 基线 ±10% |

---

## 第四批:Low 持续优化(无截止)

不在本次范围:
- L1 拆分单文件(`main.py` 700+ 行 → `pool.py` / `handler.py` / `cli.py`)
- L2 逐步加 type hints
- L5 删 `import traceback`
- L6 改用类封装 `pools` 全局

---

## Commit 拆分(8 个,按此顺序合入)

```
commit 1: fix(security): 路径白名单 + SSRF 防护
   C3 + C4 合并(同一区域 _handle_request / download_http_file)
   改: main.py 加 _is_safe_url, _is_safe_path, _NoRedirect, -allowed-dirs
   验证: tests/test_ssrf_path.py(批 3 一并加,临时手动 curl 也行)

commit 2: feat(security): API Key 认证
   C1
   改: main.py argparse -api-key/-api-key-env, Handler._check_auth, -f 模式带 header
   验证: curl + Header 401/200 测试

commit 3: chore(build): Dockerfile 升 bookworm
   C2
   改: Dockerfile base image + libgl1/libglx-mesa0 + 删 sed workaround
   验证: docker build 成功 + extract 二进制 smoke test

commit 4: fix(pool): 锁外 spawn + 1s stats 缓存
   M4 + M5
   改: submit 用 need_scale 标记, stats 加 _stats_cache
   验证: 5 并发 OCR 性能不变 + /metrics 缓存命中

commit 5: fix(handler): 健康检查真实化 + 500 脱敏
   H1 + H5
   改: do_GET health 检查 idle, /livez 新增, logger.exception 替代 str(e)
   验证: curl health/livez 在启动期/稳态/崩溃态

commit 6: fix(memory): 提交后清理 results 残留
   H2
   改: submit finally 块 pop results
   验证: 注入 worker sleep 测试 + Manager dict 大小稳定

commit 7: refactor: 命名常量 + logging 化 + M6/M7/M3/M8
   M1 + M2 + M3 + M6 + M7 + M8
   改: print→logger, 常量提取, monkey patch 提升到 worker 顶层,
       并行 spawn, requirements.txt 全 pin, _paths.py 清理
   验证: 全量测试 + 5 并发 OCR 冒烟 + worker 日志带 PID

commit 8: test: 单元测试套件
   H3
   改: 新建 tests/ 目录, conftest.py, 3 个 test_*.py, pytest.ini,
       requirements-dev.txt, CI 加 test job
   验证: pytest 全绿, 覆盖率 ≥ 50%
```

**合并建议**:
- **紧急上线**: 仅发 commit 1+2+3(Critical 三件套,~半天)
- **完整修复**: 8 个 commit 按序 squash merge,每批可单独发版

---

## Critical Files to Modify

- `D:\code\PythonCode\funASR\main.py` — 90% 改动集中在这里
- `D:\code\PythonCode\funASR\worker.py` — M1/M6(加 logging、移 monkey patch)
- `D:\code\PythonCode\funASR\_paths.py` — M3(清理死代码)
- `D:\code\PythonCode\funASR\Dockerfile` — C2(基础镜像升级)
- `D:\code\PythonCode\funASR\requirements.txt` — M8(全 pin)
- 新建 `D:\code\PythonCode\funASR\tests\` 目录(H3)
- 新建 `D:\code\PythonCode\funASR\requirements-dev.txt`(H3)
- 新建 `D:\code\PythonCode\funASR\pytest.ini`(H3)

## 已有可复用工具

- `main.py:23-28` `os.environ['PYTHONIOENCODING'] = 'utf-8'` 已有,继续用
- `main.py:317 Handler` 类框架,直接加方法
- `main.py:381-398 do_GET` 已支持多端点路由,加 `/livez` 只是 elif
- `worker.py:19-57 init_worker_processes` 模型加载逻辑不动,只移 monkey patch
- `ElasticProcessPool.stats()` 已实现原子快照(737c5d6 修过),M5 只需加缓存层

---

## 风险/依赖

### 跨批依赖
- **C1 依赖 H1 部分**: H1 改后 `/metrics` 加认证(C1 范围),health 不认证
- **批 2 临时用 logging**: H5 `logger.exception()` 需要 logging 已配,故 H5 必须配 M1 同步或在 H5 内临时 `import logging + basicConfig`
- **M6 实际行为**: Python spawn 不继承父进程 monkey patch,patch 必须在 worker.py 顶层(每个 worker 进程都执行一次)

### 行为变更风险
- **C4 默认值变更**: 之前任何绝对路径都接受,现在默认只接受 `~/uploads` 和 `/tmp`。**部署文档必须明确告知**
- **C3 SSRF 启用后**: 内网地址被拒,生产侧若需从内网下载需显式配置(超出本范围)
- **H1 health 503**: K8s readiness 错配会让流量永远不进,需配套 README 部署章节

### 测试依赖
- 多进程池难测,需 mock `mp.Process` / `mp.Manager`,可能先做 L1 拆分再补 H3(推荐)
- `_paths.py` frozen 模式测试需真实 PyInstaller build,只在 CI 跑

### 部署依赖
- Docker 升级后,CI 缓存的 `funasr-builder:latest` 镜像需清,build 时间 5min → 7min
- `pip freeze` 后版本可能引入 bug,staging 跑 1 周再推
- 升级后需设置 `-api-key`,部署文档需说明

### 性能回归
- `_is_safe_url` 的 `getaddrinfo` 增加 1-10ms,下载场景无感
- `_stats_cache` 1s TTL,Prometheus 15s scrape 无影响
- 并行 spawn(M7)反而提速

---

## Verification(端到端验证)

实施完成后的完整验证流程:

### 1. 单元测试
```bash
cd D:\code\PythonCode\funASR
D:\anaconda3\envs\funAsr\python.exe -m pip install -r requirements-dev.txt
D:\anaconda3\envs\funAsr\python.exe -m pytest tests/ -v --cov=main --cov=worker
```
期望: 全绿, 覆盖率 ≥ 50%

### 2. 集成冒烟(手动)
```bash
# 启动服务(带全部安全配置)
D:\anaconda3\envs\funAsr\python.exe main.py -port 5099 \
    -prewarm 1 -workers 4 \
    -api-key test-secret \
    -allowed-dirs ~/uploads,/tmp
```

#### 认证测试
```bash
# 不带 Header → 401
curl -X POST http://127.0.0.1:5099/ocr/identify \
  -H "Content-Type: application/json" \
  -d '{"filepath":"test.png"}'
# 期望: {"code":401,"message":"Unauthorized"}

# 带正确 Header → 200
curl -X POST http://127.0.0.1:5099/ocr/identify \
  -H "Content-Type: application/json" -H "X-API-Key: test-secret" \
  -d '{"filepath":"test.png"}'
```

#### SSRF 测试
```bash
curl -X POST http://127.0.0.1:5099/ocr/identify \
  -H "X-API-Key: test-secret" -H "Content-Type: application/json" \
  -d '{"filepath":"http://169.254.169.254/latest/meta-data/"}'
# 期望: 400, "URL not allowed"
```

#### 路径穿越测试
```bash
curl -X POST http://127.0.0.1:5099/ocr/identify \
  -H "X-API-Key: test-secret" -H "Content-Type: application/json" \
  -d '{"filepath":"/etc/passwd.png"}'
# 期望: 400, "Path not allowed"
```

#### 健康检查测试
```bash
# 服务启动但 worker 还在加载
curl http://127.0.0.1:5099/funasr/health
# 期望: 503

# wait_ready 完成后
curl http://127.0.0.1:5099/funasr/health
# 期望: 200

# /livez 永远 200
curl http://127.0.0.1:5099/livez
# 期望: 200
```

#### /metrics 测试(需认证)
```bash
curl http://127.0.0.1:5099/metrics
# 期望: 401

curl -H "X-API-Key: test-secret" http://127.0.0.1:5099/metrics
# 期望: 200 + Prometheus 格式
```

### 3. Dockerfile 构建验证
```bash
docker build --platform linux/amd64 -t funasr-builder:test .
docker create --name funasr-tmp funasr-builder:test
docker cp funasr-tmp:/build/dist/funasr ./funasr-test
docker rm funasr-tmp
./funasr-test -f README.md  # 简单 smoke
```

### 4. 并发回归(对比 737c5d6 基线)
```bash
# 5 并发 OCR 端到端测试
python _test_regress.py
# 期望: 5/5 成功, scale_events=5, in_flight=0
```

### 5. 性能回归(P50/P99 延迟)
对比 737c5d6 commit 时的基线数据,允许 ±10% 偏差。
