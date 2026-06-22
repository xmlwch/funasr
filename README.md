# FunASR 语音识别 + OCR 服务

基于阿里达摩院 **SenseVoiceSmall** 语音识别模型和 **PaddleOCR PP-OCRv4** 文字识别模型的 HTTP 服务。

## 功能

| 服务 | 模型 | 支持语言 |
|------|------|----------|
| 语音识别 | SenseVoiceSmall | 中文、粤语、英语、日语、韩语 |
| 文字识别 | PaddleOCR PP-OCRv4 | 中文、英文 |

## 下载

在 [Releases](../../releases) 页面下载：

| 文件 | 说明 | 大小 |
|------|------|------|
| `funasr-linux-x86_64` | Linux x86_64 二进制 | ~350MB |
| `funasr-linux-aarch64` | Linux ARM64 二进制 | ~350MB |
| `funasr-windows-x86_64.exe` | Windows x86_64 二进制 | ~350MB |
| `models.zip` | 模型文件 | ~1.2GB |

需要 glibc ≥ 2.28（CentOS 8+ / Ubuntu 20.04+ / Debian 10+）。

## 准备模型

下载 `models.zip` 并解压到二进制同目录下：

```bash
# 解压模型
unzip models.zip -d ./model/

# 目录结构
model/
├── model.onnx              # ASR 主模型
├── model_quant.onnx        # ASR 量化模型
├── tokens.json             # ASR 词表
├── config.yaml             # ASR 配置
├── am.mvn                  # ASR 配置
├── chn_jpn_yue_eng_ko_spectok.bpe.model  # BPE 模型
├── configuration.json       # ASR 配置
└── paddleocr/             # OCR 模型
    ├── det/                # 检测模型
    └── rec/                # 识别模型
```

或使用环境变量指定模型路径：

```bash
# ASR 模型
export FUNASR_MODEL_DIR=/path/to/asr/models

# OCR 模型
export FUNASR_OCR_MODEL_DIR=/path/to/ocr/models
```

## 使用

### 启动服务

```bash
# Linux / macOS
chmod +x funasr-linux-x86_64
./funasr-linux-x86_64 -port 5001

# Windows
funasr-windows-x86_64.exe -port 5001
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-host` | `127.0.0.1` | 绑定 IP，`0.0.0.0` 允许外部访问 |
| `-port` | `5001` | 监听端口 |

### CLI 调用

命令行直接识别，自动根据文件类型选择服务：

```bash
# 语音识别（自动识别）
./funasr -f audio.wav
./funasr -f audio.mp3

# OCR 识别（自动识别）
./funasr -f image.png
./funasr -f photo.jpg

# 识别网络文件
./funasr -f http://example.com/audio.mp3
./funasr -f http://example.com/image.png
```

支持的音频格式：`.wav`, `.mp3`, `.m4a`, `.flac`, `.ogg`, `.aac`, `.wma`
支持的图片格式：`.png`, `.jpg`, `.jpeg`, `.bmp`, `.gif`, `.tiff`, `.webp`

### HTTP 调用

#### 语音识别

```bash
curl -X POST http://127.0.0.1:5001/funasr/identify \
  -H "Content-Type: application/json" \
  -d '{"filepath": "/path/to/audio.wav"}'
```

#### 文字识别

```bash
curl -X POST http://127.0.0.1:5001/ocr/identify \
  -H "Content-Type: application/json" \
  -d '{"filepath": "/path/to/image.png"}'
```

#### 返回格式

```json
{
  "code": 200,
  "message": "识别成功",
  "data": "识别结果文本",
  "duration": 0.165
}
```

### 健康检查

```bash
# ASR 健康检查
curl http://127.0.0.1:5001/funasr/health

# OCR 健康检查
curl http://127.0.0.1:5001/ocr/health
```

## 限制

| 限制项 | 值 | 说明 |
|--------|-----|------|
| 最大请求体 | 100MB | 超过返回 413 |
| 推理超时 | 300秒 | 超过返回 408 |
| 下载超时 | 60秒 | 远程文件下载 |

## 注意事项

- 二进制内嵌 Python 和所有依赖，目标机器不需要装任何东西
- 模型文件约 1.2GB，需下载并解压
- 服务启动时预加载模型，之后识别秒级返回
- `.env` 文件由服务自动生成，供 CLI 模式使用
