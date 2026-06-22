# PPOCR 集成实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 FunASR 项目中新增 PPOCR OCR HTTP 服务，提供与语音识别一致的 API 风格

**Architecture:** 在现有 `main.py` 中新增 `PPOCR` 类，单例模式，延迟加载。在 `Handler` 中新增 `/ocr/identify` 和 `/ocr/health` 路由。

**Tech Stack:** Python, rapidocr_onnxruntime, ThreadingHTTPServer

---

## 文件改动概览

| 文件 | 改动类型 |
|------|----------|
| `main.py` | 修改：新增 PPOCR 类，扩展 Handler 路由 |
| `requirements.txt` | 修改：新增 rapidocr_onnxruntime |
| `.github/workflows/build.yml` | 修改：新增 rapidocr_onnxruntime 打包参数 |
| `Dockerfile` | 修改：新增 --hidden-import=rapidocr_onnxruntime |

---

## Task 1: 添加依赖

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: 添加 rapidocr_onnxruntime 到 requirements.txt**

打开 `requirements.txt`，新增一行：
```
rapidocr_onnxruntime
```

完整文件内容：
```
funasr-onnx
funasr
onnxruntime
rapidocr_onnxruntime
```

- [ ] **Step 2: 提交更改**

```bash
git add requirements.txt
git commit -m "feat: add rapidocr_onnxruntime dependency for OCR

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: 实现 PPOCR 类

**Files:**
- Modify: `main.py`（在 FunASR 类后新增 PPOCR 类）

- [ ] **Step 1: 在 main.py 中添加 PPOCR 类**

在 `FunASR` 类后（大约第 82 行后）添加：

```python
class PPOCR:
    _instance = None
    _lock = threading.Lock()
    _initialized = False

    def __init__(self):
        if not self.__class__._initialized:
            with self.__class__._lock:
                if not self.__class__._initialized:
                    self.__class__._initialized = True
                    from rapidocr_onnxruntime import RapidOCR
                    self.ocr = RapidOCR()
                    print("OCR 模型初始化成功")

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    def _download_http(url):
        fd, tmp_path = tempfile.mkstemp(suffix="_image")
        os.close(fd)
        try:
            with urllib.request.urlopen(url, timeout=30) as resp, open(tmp_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        except Exception:
            os.unlink(tmp_path)
            raise
        return tmp_path

    def _generate_text(self, image_path):
        if not os.path.exists(image_path):
            raise FileNotFoundError("文件不存在: %s" % image_path)
        result, elapse = self.ocr(image_path)
        if result is None:
            return ""
        # 合并所有识别结果，用换行分隔
        texts = [item[1] for item in result]
        return "\n".join(texts)

    async def get_text_content(self, image_path):
        tmp_path = None
        try:
            loop = asyncio.get_running_loop()
            if image_path.startswith(("http://", "https://")):
                real_path = await loop.run_in_executor(None, self._download_http, image_path)
                tmp_path = real_path
            else:
                real_path = image_path
            text = await loop.run_in_executor(None, self._generate_text, real_path)
            return text
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
```

- [ ] **Step 2: 提交更改**

```bash
git add main.py
git commit -m "feat: add PPOCR class for OCR HTTP service

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: 扩展 Handler 路由

**Files:**
- Modify: `main.py`（修改 Handler 类的 do_POST 和 do_GET 方法）

- [ ] **Step 1: 修改 do_POST 方法，添加 /ocr/identify 路由**

找到 `Handler` 类的 `do_POST` 方法（约第 86-105 行），修改为：

```python
def do_POST(self):
    if self.path == '/funasr/identify':
        try:
            start_time = time.time()
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length).decode('utf-8'))
            filepath = body.get('filepath')
            if not filepath:
                self._json(400, {'code': 400, 'message': '缺少 filepath 参数', 'data': None})
                return
            text = asyncio.run(FunASR().get_audio_content(filepath))
            duration = time.time() - start_time
            self._json(200, {'code': 200, 'message': '识别成功', 'data': text, 'duration': duration})
        except FileNotFoundError as e:
            self._json(400, {'code': 400, 'message': str(e), 'data': None})
        except Exception as e:
            print(e)
            self._json(400, {'code': 400, 'message': '当前系统繁忙，请稍后重试', 'data': None})
    elif self.path == '/ocr/identify':
        try:
            start_time = time.time()
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length).decode('utf-8'))
            filepath = body.get('filepath')
            if not filepath:
                self._json(400, {'code': 400, 'message': '缺少 filepath 参数', 'data': None})
                return
            text = asyncio.run(PPOCR().get_text_content(filepath))
            duration = time.time() - start_time
            self._json(200, {'code': 200, 'message': '识别成功', 'data': text, 'duration': duration})
        except FileNotFoundError as e:
            self._json(400, {'code': 400, 'message': str(e), 'data': None})
        except Exception as e:
            print(e)
            self._json(400, {'code': 400, 'message': '当前系统繁忙，请稍后重试', 'data': None})
    else:
        self._json(404, {'code': 404, 'message': '未找到路由', 'data': None})
```

- [ ] **Step 2: 修改 do_GET 方法，添加 /ocr/health 路由**

找到 `Handler` 类的 `do_GET` 方法（约第 107-111 行），修改为：

```python
def do_GET(self):
    if self.path == '/funasr/health':
        self._json(200, {'code': 200, 'status': 'ok'})
    elif self.path == '/ocr/health':
        self._json(200, {'code': 200, 'status': 'ok'})
    else:
        self._json(405, {'code': 405, 'message': '仅支持 POST', 'data': None})
```

- [ ] **Step 3: 提交更改**

```bash
git add main.py
git commit -m "feat: add /ocr/identify and /ocr/health routes to Handler

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: 更新打包配置

**Files:**
- Modify: `Dockerfile`
- Modify: `.github/workflows/build.yml`

- [ ] **Step 1: 更新 Dockerfile**

找到 Dockerfile 中的 `pyinstaller` 命令，添加 `--hidden-import=rapidocr_onnxruntime \`

修改后：
```dockerfile
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt pyinstaller && \
    pyinstaller --onefile \
      --add-binary "$(python -c 'import onnxruntime; print(onnxruntime.__path__[0])')/capi/*.so*:onnxruntime/capi" \
      --collect-submodules torch \
      --hidden-import=torch \
      --hidden-import=torchaudio \
      --hidden-import=funasr_onnx \
      --hidden-import=funasr \
      --hidden-import=librosa \
      --hidden-import=soundfile \
      --hidden-import=rapidocr_onnxruntime \
      --name funasr \
      main.py
```

- [ ] **Step 2: 更新 build.yml 的 Windows 构建参数**

找到 Windows 构建的 pyinstaller 命令，添加 `--collect-all rapidocr_onnxruntime`

修改后（约第 50 行）：
```yaml
- name: Build with PyInstaller
  run: |
    pyinstaller --onefile --name funasr --collect-all torch --collect-all torchaudio --collect-all rapidocr_onnxruntime --hidden-import=funasr_onnx --hidden-import=funasr --hidden-import=librosa --hidden-import=soundfile main.py
```

- [ ] **Step 3: 提交更改**

```bash
git add Dockerfile .github/workflows/build.yml
git commit -m "chore: add rapidocr_onnxruntime to build configuration

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: 验证测试

**Files:**
- Test: 本地环境测试

- [ ] **Step 1: 安装依赖**

```bash
pip install -r requirements.txt
```

- [ ] **Step 2: 启动服务（后台模式）**

```bash
python main.py -port 5001 &
```

- [ ] **Step 3: 测试 FunASR 健康检查**

```bash
curl http://127.0.0.1:5001/funasr/health
# 期望返回: {"code": 200, "status": "ok"}
```

- [ ] **Step 4: 测试 PPOCR 健康检查**

```bash
curl http://127.0.0.1:5001/ocr/health
# 期望返回: {"code": 200, "status": "ok"}
```

- [ ] **Step 5: 测试 PPOCR 识别（准备一张测试图片）**

```bash
curl -X POST http://127.0.0.1:5001/ocr/identify \
  -H "Content-Type: application/json" \
  -d '{"filepath": "/path/to/test/image.png"}'
# 期望返回: {"code": 200, "message": "识别成功", "data": "...", "duration": ...}
```

---

## 自检清单

- [ ] spec 覆盖：所有设计文档中的功能都有对应实现
- [ ] 占位符检查：无 TBD/TODO/未完成内容
- [ ] 类型一致性：PPOCR 类方法名与 FunASR 类一致
- [ ] 路由一致性：OCR 路由与 ASR 路由风格一致
- [ ] 错误处理：FileNotFoundError 和通用异常处理到位
