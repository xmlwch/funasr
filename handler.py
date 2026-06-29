# -*- coding: utf-8 -*-
"""HTTP 请求处理 + 共享全局状态

提供:
- pools dict:model_type → ElasticProcessPool 映射(__main__ 填充)
- _ALLOWED_DIRS:路径白名单展开后的绝对路径列表
- ROUTES:HTTP path → model_type 路由表
- Handler:BaseHTTPRequestHandler 子类,处理 /funasr/identify、/ocr/identify、
  /metrics、/health、/livez、/ocr/health

【L1 拆分】handler.py 是 main.py 的"前端层",安全/池逻辑在 security.py/pool.py。
"""
import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
from http.server import BaseHTTPRequestHandler

from security import _is_safe_path, download_http_file

logger = logging.getLogger('funasr.handler')

# 路由表:HTTP path 前缀 → 模型类型
ROUTES = {
    '/funasr/identify': 'asr',
    '/ocr/identify': 'ocr',
}

# 池注册表:model_type → ElasticProcessPool。__main__ 启动时填充。
pools: dict = {}

# 路径白名单:__main__ 启动前展开为绝对路径列表
_ALLOWED_DIRS = []

# 支持的文件后缀
AUDIO_EXTS = {'.wav', '.mp3', '.m4a', '.flac', '.ogg', '.aac', '.wma', '.opus', '.ape', '.ac3'}
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff', '.webp', '.tif', '.jfif'}

# 请求体最大大小(防 DoS)
MAX_CONTENT_LENGTH = 100 * 1024 * 1024

# HTTP server 排队连接数
HTTP_REQUEST_QUEUE_SIZE = 128


class Handler(BaseHTTPRequestHandler):
    """funasr OCR HTTP 端点处理

    主要端点:
    - POST /funasr/identify → ASR 推理
    - POST /ocr/identify   → OCR 推理
    - GET  /funasr/health  → ASR 池 readiness(503 if no idle)
    - GET  /ocr/health     → OCR 池 readiness(503 if no idle)
    - GET  /livez          → 永远 200(K8s liveness probe)
    - GET  /metrics        → Prometheus 格式(需 API Key)

    API Key 通过启动时 -api-key 或 -api-key-env 设置,
    未设置时不强制认证(开发模式)。
    """
    request_queue_size = HTTP_REQUEST_QUEUE_SIZE
    _api_key = None  # __main__ 启动时注入

    @classmethod
    def set_api_key(cls, key):
        cls._api_key = key

    def _check_auth(self) -> bool:
        """校验 X-API-Key Header(防时序攻击用 hmac.compare_digest)

        未设置 _api_key → 不校验(开发模式)
        已设置 → 必须带正确密钥
        """
        if not Handler._api_key:
            return True
        return hmac.compare_digest(
            self.headers.get('X-API-Key', ''), Handler._api_key)

    def do_POST(self):
        if not self._check_auth():
            self.send_response(401)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('WWW-Authenticate', 'X-API-Key')
            self.end_headers()
            self.wfile.write(json.dumps(
                {'code': 401, 'message': 'Unauthorized', 'data': None},
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
        """拒绝条件收窄:只在"完全无可用 worker"时才拒
        busy 不再是拒绝理由 — busy 时请求进 submit() 排队 + 触发主动扩容
        """
        s = target_pool.stats()
        if s['alive'] == 0 or s['alive'] == s['dead']:
            self._json(503, {'code': 503,
                             'message': '服务正在启动/无可用 worker,请稍后重试',
                             'data': None})
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

            ext = os.path.splitext(filepath)[1].lower()
            if service_type == 'asr' and ext not in AUDIO_EXTS:
                self._json(400, {'code': 400,
                                 'message': f'ASR 端点不支持文件类型: {ext},支持的格式: {", ".join(sorted(AUDIO_EXTS))}',
                                 'data': None}); return
            if service_type == 'ocr' and ext not in IMAGE_EXTS:
                self._json(400, {'code': 400,
                                 'message': f'OCR 端点不支持文件类型: {ext},支持的格式: {", ".join(sorted(IMAGE_EXTS))}',
                                 'data': None}); return

            if filepath.startswith(("http://", "https://")):
                suffix = "_audio" if service_type == "asr" else "_image"
                tmp_path = download_http_file(filepath, suffix)
                real_path = tmp_path
            else:
                real_path = _is_safe_path(filepath, _ALLOWED_DIRS)
                if os.path.getsize(real_path) > MAX_CONTENT_LENGTH:
                    raise ValueError("文件大小超过限制")

            text = target_pool.submit(service_type, real_path)
            duration = time.time() - start_time
            self._json(200, {'code': 200, 'message': '识别成功',
                             'data': text, 'duration': round(duration, 3)})

        except TimeoutError:
            self._json(408, {'code': 408, 'message': '推理超时', 'data': None})
        except (ValueError, FileNotFoundError) as e:
            logger.warning("client error (path=%s): %s", self.path, e)
            self._json(400, {'code': 400, 'message': str(e), 'data': None})
        except Exception as e:
            if "正在关闭" in str(e):
                self._json(503, {'code': 503, 'message': '服务正在关闭', 'data': None})
            else:
                logger.exception("unhandled error in _handle_request (path=%s, client=%s)",
                                 self.path, self.address_string())
                self._json(500, {'code': 500, 'message': '内部错误,请稍后重试', 'data': None})
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def do_GET(self):
        if self.path == '/metrics':
            if not self._check_auth():
                self._json(401, {'code': 401, 'message': 'Unauthorized', 'data': None}); return
            lines = []
            for pool in pools.values():
                s = pool.stats()
                model = s['model_type']
                for key in ('alive', 'max', 'min', 'in_flight', 'idle',
                            'busy', 'loading', 'dead', 'scale_events'):
                    lines.append(f'funasr_pool_{key}{{model="{model}"}} {s[key]}')
            body = ('\n'.join(lines) + '\n').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ['/funasr/health', '/ocr/health']:
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
