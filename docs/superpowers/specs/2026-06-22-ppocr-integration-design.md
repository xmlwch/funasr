# PPOCR 集成设计文档

## 概述

在 FunASR 语音识别服务中新增 PPOCR OCR HTTP 服务，保持 API 风格一致。

## 技术选型

- **OCR 引擎**: `rapidocr_onnxruntime`
- **原因**: 轻量 (~50MB)、推理快、ONNX 推理、兼容性好

## 架构设计

```
main.py
├── FunASR 类（保持不变）
│   └── /funasr/identify → 语音识别
│
└── 新增 PPOCR 类
    ├── 单例模式，延迟加载
    ├── /ocr/identify → OCR 识别
    └── /ocr/health → 健康检查
```

## API 设计

### OCR 识别

**端点**: `POST /ocr/identify`

**请求体**:
```json
{
  "filepath": "/path/to/image.png"
}
```

`filepath` 支持：
- 本地文件路径
- 远程 URL (`http://`, `https://`)

**响应**:
```json
{
  "code": 200,
  "message": "识别成功",
  "data": "图片中的文字",
  "duration": 0.165
}
```

**错误响应**:
```json
{
  "code": 400,
  "message": "文件不存在或不支持的格式",
  "data": null
}
```

### 健康检查

**端点**: `GET /ocr/health`

**响应**:
```json
{
  "code": 200,
  "status": "ok"
}
```

## 文件改动

### 1. main.py

**新增 PPOCR 类**:
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

    def _generate_text(self, image_path):
        if not os.path.exists(image_path):
            raise FileNotFoundError("文件不存在: %s" % image_path)
        result, elapse = self.ocr(image_path)
        if result is None:
            return ""
        # 合并所有识别结果
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

**Handler 扩展**:
- `do_POST` 中新增 `/ocr/identify` 路由
- `do_GET` 中新增 `/ocr/health` 路由

**服务启动**:
- OCR 模型在首次调用时延迟加载（非启动时）
- 或者与 FunASR 同时在服务模式时加载

### 2. requirements.txt

新增：
```
rapidocr_onnxruntime
```

### 3. .github/workflows/build.yml

**Linux 构建 (Dockerfile)**:
```dockerfile
--hidden-import=rapidocr_onnxruntime
```

**Windows 构建**:
```bash
--collect-all rapidocr_onnxruntime
```

## 实现顺序

1. 添加 `rapidocr_onnxruntime` 到 `requirements.txt`
2. 在 `main.py` 中实现 `PPOCR` 类
3. 在 `Handler` 中添加 `/ocr/identify` 和 `/ocr/health` 路由
4. 更新 `Dockerfile` 添加 `--hidden-import=rapidocr_onnxruntime`
5. 更新 `build.yml` 的 Windows 构建参数
6. 测试验证

## 注意事项

- OCR 模型自动下载（rapidocr 会自动下载所需的 ONNX 模型）
- 图片格式支持：PNG、JPG、BMP 等常见格式
- 远程文件下载复用 `FunASR._download_http` 方法
- 统一错误处理，格式与语音识别一致
