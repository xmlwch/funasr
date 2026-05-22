# FunASR HTTP 语音识别服务

基于阿里达摩院 [FunASR](https://github.com/modelscope/FunASR) SenseVoiceSmall 模型的本地语音识别 HTTP 服务。支持中文、粤语、英语、日语、韩语等多语种识别。

## 快速开始

### 1. 准备模型

```bash
# 方式一：自动下载（首次运行）
export FUNASR_MODEL_DIR="iic/SenseVoiceSmall"

# 方式二：手动下载后指定本地路径
# 模型下载地址：https://www.modelscope.cn/models/iic/SenseVoiceSmall
export FUNASR_MODEL_DIR="./model"
```

### 2. 启动服务

```bash
# CPU 环境安装依赖
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

# 启动（默认监听 127.0.0.1:5001）
python main.py

# 自定义端口，允许外部访问
python main.py -host 0.0.0.0 -port 8080
```

### 3. 调用服务

```bash
# CLI 模式（自动读取 .env 连接本机服务）
python main.py -f /path/to/audio.wav
python main.py -f http://example.com/audio.mp3

# HTTP 接口
curl -X POST http://127.0.0.1:5001/funasr/identify \
  -H "Content-Type: application/json" \
  -d '{"filepath": "/path/to/audio.wav"}'

# 健康检查
curl http://127.0.0.1:5001/funasr/health
```

## 接口说明

### POST /funasr/identify

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| filepath | string | 是 | 音频文件路径或 HTTP URL |

返回示例：
```json
{
  "code": 200,
  "message": "识别成功",
  "data": "开饭时间早上九点至下午五点",
  "duration": 0.165
}
```

### GET /funasr/health

返回 `{"code": 200, "status": "ok"}`

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-host` | 127.0.0.1 | 绑定 IP，设为 0.0.0.0 允许外部访问 |
| `-port` | 5001 | 监听端口 |
| `-f` | 无 | 指定音频文件直接识别（CLI 模式，需服务已启动） |

## 预编译二进制

通过 GitHub Actions 自动构建，在 [Releases](../../releases) 页面下载：

- `linux-x86_64` — Linux x86_64（glibc ≥ 2.28）
- `linux-aarch64` — Linux ARM64（glibc ≥ 2.28）
- `windows-x86_64` — Windows x86_64

```bash
# Linux
chmod +x main-linux-x86_64
export FUNASR_MODEL_DIR=./model
./main-linux-x86_64 -port 5001

# Windows
set FUNASR_MODEL_DIR=./model
main-windows-x86_64.exe -port 5001
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FUNASR_MODEL_DIR` | 程序同目录下的 `model/` | 模型目录路径或 ModelScope 模型 ID |

## 依赖

- Python ≥ 3.8
- funasr-onnx
- funasr
- onnxruntime
- torch / torchaudio（CPU 版即可）

## 许可证

本项目代码仅供学习和研究使用。模型许可请参考 [FunASR MODEL LICENSE](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)。
