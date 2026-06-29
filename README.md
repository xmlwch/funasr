# FunASR 语音识别 + OCR 服务

基于阿里达摩院 **SenseVoiceSmall** 语音识别模型和 **PaddleOCR PP-OCRv4** 文字识别模型的 HTTP 服务。

## 功能

| 服务 | 模型 | 支持语言 |
|------|------|----------|
| 语音识别 | SenseVoiceSmall | 中文、粤语、英语、日语、韩语 |
| 文字识别 | PaddleOCR PP-OCRv4 | 中文、英文 |

## 下载

在 [Releases](../../releases) 页面下载对应平台的二进制文件。

需要 glibc ≥ 2.28（CentOS 8+ / Ubuntu 20.04+ / Debian 10+）。

## 准备模型

### ASR 模型（SenseVoiceSmall）

> ⚠️ 注意:`iic/SenseVoiceSmall` 是 **PyTorch** 版本,本项目用的是 **ONNX** 版本 `iic/SenseVoiceSmall-onnx`,请勿下错。

从 [ModelScope 官方 ONNX 仓库](https://www.modelscope.cn/models/iic/SenseVoiceSmall-onnx/summary) 下载,推荐直接用 wget:

```bash
mkdir -p model && cd model

BASE="https://www.modelscope.cn/api/v1/models/iic/SenseVoiceSmall-onnx/repo?Revision=master&FilePath="

# INT8 量化版(funasr-onnx 默认加载的就是这个,CPU 推荐)+ 配套文件
for f in model_quant.onnx tokens.json config.yaml am.mvn configuration.json chn_jpn_yue_eng_ko_spectok.bpe.model; do
    wget "${BASE}${f}"
done

# 可选:FP32 原版
wget "${BASE}model.onnx"
```

或用 ModelScope SDK:
```python
from modelscope import snapshot_download
snapshot_download("iic/SenseVoiceSmall-onnx", local_dir="./model")
```

下载后目录结构:

```
model/
├── model.onnx            # FP32(~250MB,可选)
├── model_quant.onnx      # INT8 量化(~130MB,推荐)
├── tokens.json
├── config.yaml
├── am.mvn
├── chn_jpn_yue_eng_ko_spectok.bpe.model
└── configuration.json
```

### OCR 模型（PaddleOCR PP-OCRv4）

下载并解压：
```bash
# 检测模型
curl -L -o det.tar https://paddleocr.bj.bcebos.com/PP-OCRv4/chinese/ch_PP-OCRv4_det_infer.tar
tar -xf det.tar && mv ch_PP-OCRv4_det_infer det/

# 识别模型
curl -L -o rec.tar https://paddleocr.bj.bcebos.com/PP-OCRv4/chinese/ch_PP-OCRv4_rec_infer.tar
tar -xf rec.tar && mv ch_PP-OCRv4_rec_infer rec/

# 方向分类模型（use_angle_cls=True 必需）
curl -L -o cls.tar https://paddleocr.bj.bcebos.com/PP-OCRv4/chinese/ch_PP-OCRv4_cls_infer.tar
tar -xf cls.tar && mv ch_PP-OCRv4_cls_infer cls/
```

最终目录结构：
```
model/
├── model.onnx
├── model_quant.onnx
├── tokens.json
├── config.yaml
├── am.mvn
├── chn_jpn_yue_eng_ko_spectok.bpe.model
├── configuration.json
└── paddleocr/
    ├── det/
    ├── rec/
    └── cls/        # 文本方向分类（必需）
```

或使用环境变量指定模型路径：

```bash
export FUNASR_MODEL_DIR=/path/to/asr/models
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
| `-workers` | `16` | 每池最大 worker 数 |
| `-asr-workers` / `-ocr-workers` | 同 `-workers` | per-pool 覆盖 |
| `-prewarm` | `4` | 启动时预热 worker 数 |
| `-asr-prewarm` / `-ocr-prewarm` | 同 `-prewarm` | per-pool 覆盖 |
| `-min-workers` | `1` | 空闲后最少保留 worker(防冷启动) |
| `-asr-min-workers` / `-ocr-min-workers` | 同 `-min-workers` | per-pool 覆盖 |
| `-max-queue` | `200` | 队列上限,超过返 503 |
| `-asr-max-queue` / `-ocr-max-queue` | 同 `-max-queue` | per-pool 覆盖 |
| `-idle` | `300` | 空闲超时后缩容(秒) |
| `-api-key` | `None` | API 密钥(启用后客户端需带 `X-API-Key` Header) |
| `-api-key-env` | `None` | 从环境变量读 API 密钥(避免 ps 暴露) |
| `-allowed-dirs` | `~/uploads,/tmp` | 路径白名单,支持 ~、`$VAR`、glob(`*`/`?`/`**`) |
| `-allowed-internal-hosts` | `127.0.0.1,localhost,::1` | URL 白名单绕过 SSRF,详见下文 |

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

支持的音频格式：`.wav`, `.mp3`, `.m4a`, `.flac`, `.ogg`, `.aac`, `.wma`, `.opus`, `.ape`, `.ac3`
支持的图片格式：`.png`, `.jpg`, `.jpeg`, `.bmp`, `.gif`, `.tiff`, `.webp`, `.tif`, `.jfif`

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
# ASR 健康检查(返回 503 当 ASR 池无 idle worker)
curl http://127.0.0.1:5001/funasr/health
# → 200 {"code":200,"status":"ok","stats":{...}} 或
# → 503 {"code":503,"status":"not_ready","message":"no idle workers","stats":{...}}

# OCR 健康检查
curl http://127.0.0.1:5001/ocr/health

# 进程活性探针(永远 200,用于 K8s liveness)
curl http://127.0.0.1:5001/livez

# Prometheus 指标(需 X-API-Key)
curl -H "X-API-Key: your-key" http://127.0.0.1:5001/metrics
```

### 路径白名单 `-allowed-dirs`

接受逗号分隔路径,支持 `~` 展开、环境变量、glob 通配:

```bash
# 字面路径
./funasr -allowed-dirs '~/uploads,/tmp'

# 环境变量
DATA_ROOT=/data ./funasr -allowed-dirs '$DATA_ROOT/incoming'

# 单层通配:~/uploads/2024-Q1, 2024-Q2 ...
./funasr -allowed-dirs '~/uploads/2024-*'

# 递归通配:~/uploads 全部后代
./funasr -allowed-dirs '~/uploads/**'

# 混合(逗号分隔)
./funasr -allowed-dirs '~/uploads,/tmp,$DATA_ROOT/shared,~/uploads/2024-*/incoming'
```

路径白名单防任意文件读取:用户请求的路径 `realpath` 后必须匹配其中某条。
展开 `*`/`**` 默认最多 1000 个,超过报错(防 DoS)。

### API Key 认证

未设 `-api-key` / `-api-key-env` 时**不强制**(开发模式,127.0.0.1 双重防御)。
生产部署建议:

```bash
# 推荐:从环境变量注入,避免 ps 暴露
export FUNASR_API_KEY=$(openssl rand -hex 32)
./funasr -host 0.0.0.0 -api-key-env FUNASR_API_KEY
```

客户端必须带 `X-API-Key` Header,否则 401(POST 端点),`/metrics` 也需认证。
`/livez` 与 `/health` 永不认证(K8s 探针要求)。

### URL 白名单 `-allowed-internal-hosts`(SSRF bypass)

请求体里的 `filepath` 可以是 HTTP(S) URL,默认会被 SSRF 防御拒绝指向内网/metadata 的 URL。

**默认**:`127.0.0.1,localhost,::1` — 同机 HTTP 服务开箱即用(IPv4 + IPv6 localhost)。

**生产内网场景**:显式指定可信主机,绕开 SSRF IP 段检查。支持 3 种格式:

```bash
# 单个 IP
./funasr -allowed-internal-hosts '192.168.1.100'

# hostname 字面量
./funasr -allowed-internal-hosts 'internal.api.local'

# 整个 LAN 段(CIDR)
./funasr -allowed-internal-hosts '192.168.0.0/16'

# IPv6 段
./funasr -allowed-internal-hosts '::1/128,fe80::/10'

# 混合:IPv4 + IPv6 + hostname + CIDR
./funasr -allowed-internal-hosts '127.0.0.1,::1,localhost,internal.api.local,10.0.0.0/8,192.168.0.0/16'

# 多段(企业各网段)
./funasr -allowed-internal-hosts '192.168.0.0/16,10.0.0.0/8,172.16.0.0/12'
```

**安全护栏**:
- 信任列表命中 → 跳过 IP 段黑名单(loopback / private / link-local / reserved / multicast)
- **但** `metadata.google.internal` / `metadata` / `kubernetes.default.svc` 等**永远拒**(hard blacklist,信任列表也无法 bypass)
- 可信项必须可被 `ipaddress` 解析;无效 CIDR 会被 logger.warning + skip
- 至少一项需是合法的 hostname / IP / CIDR,否则该项被丢弃(不进 trust set)

**生产推荐**:**不要**用 `0.0.0.0/0`(那等于禁 SSRF)— 精确到企业实际网段。

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
