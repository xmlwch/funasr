# FunASR 语音识别服务

基于阿里达摩院 SenseVoiceSmall 模型的语音识别 HTTP 服务，支持中文、粤语、英语、日语、韩语等。

## 下载

在 [Releases](../../releases) 页面下载对应平台的二进制文件：

| 平台 | 文件名 |
|------|--------|
| Linux x86_64 | `funasr-linux-x86_64` |
| Linux aarch64 | `funasr-linux-aarch64` |
| Windows x86_64 | `funasr-windows-x86_64.exe` |

需要 glibc ≥ 2.28（CentOS 8+ / Ubuntu 20.04+ / Debian 10+）。

## 准备模型

模型文件需单独下载，两种方式：

```bash
# 方式一：启动时自动下载
export FUNASR_MODEL_DIR="iic/SenseVoiceSmall"

# 方式二：手动下载后放本地
# 从 https://www.modelscope.cn/models/iic/SenseVoiceSmall 下载
# 放到二进制同目录下的 model/ 文件夹
```

## 使用

### 启动服务

```bash
# Linux / macOS
chmod +x funasr-linux-x86_64
export FUNASR_MODEL_DIR=./model
./funasr-linux-x86_64 -port 5001

# Windows
set FUNASR_MODEL_DIR=./model
funasr-windows-x86_64.exe -port 5001
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-host` | `127.0.0.1` | 绑定 IP，`0.0.0.0` 允许外部访问 |
| `-port` | `5001` | 监听端口 |

### CLI 调用

服务启动后，命令行直接识别（无需额外指定 host/port，自动读取 `.env` 文件）：

```bash
# 识别本地文件
./funasr-linux-x86_64 -f /path/to/audio.wav

# 识别网络文件
./funasr-linux-x86_64 -f http://example.com/audio.mp3
```

### HTTP 调用

```bash
curl -X POST http://127.0.0.1:5001/funasr/identify \
  -H "Content-Type: application/json" \
  -d '{"filepath": "/path/to/audio.wav"}'
```

返回：

```json
{
  "code": 200,
  "message": "识别成功",
  "data": "开饭时间早上九点至下午五点",
  "duration": 0.165
}
```

### 健康检查

```bash
curl http://127.0.0.1:5001/funasr/health
# {"code": 200, "status": "ok"}
```

## 注意事项

- 二进制内嵌 Python 和所有依赖，目标机器不需要装任何东西
- 模型文件约 900MB，首次下载需要几分钟
- 服务启动时预加载模型，之后识别秒级返回
- `.env` 文件由服务自动生成，供 CLI 模式使用
