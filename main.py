import os
import re
import sys
import json
import time
import asyncio
import shutil
import argparse
import tempfile
import threading
import urllib.request
# 生产环境保护配置
MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 最大请求体 100MB
INFERENCE_TIMEOUT = 300  # 推理超时 300 秒
DOWNLOAD_TIMEOUT = 60   # 下载超时 60 秒

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from funasr.utils.postprocess_utils import rich_transcription_postprocess
from funasr_onnx import SenseVoiceSmall


class FunASR:
    _instance = None
    _lock = threading.Lock()
    _initialized = False

    def __init__(self):
        if not self.__class__._initialized:
            with self.__class__._lock:
                if not self.__class__._initialized:
                    self.__class__._initialized = True
                    if getattr(sys, 'frozen', False):
                        base_dir = os.path.dirname(os.path.abspath(sys.executable))
                    else:
                        base_dir = os.path.dirname(os.path.abspath(__file__))
                    model_dir = os.environ.get("FUNASR_MODEL_DIR", os.path.join(base_dir, "model"))
                    self.model = SenseVoiceSmall(model_dir, batch_size=1, quantize=True, intra_op_num_threads=4)
                    # self.model = SenseVoiceSmall(model_dir, batch_size=10, quantize=True, intra_op_num_threads=64)
                    print("✓ 语音模型加载完成 (SenseVoiceSmall)")

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    def _clean_text(text):
        return rich_transcription_postprocess(text)

    def _generate_audio(self, audio_path):
        if not os.path.exists(audio_path):
            raise FileNotFoundError("文件不存在: %s" % audio_path)
        res = self.model(audio_path, language="auto", use_itn=True)
        if isinstance(res, list) and len(res) > 0:
            return self._clean_text(res[0])
        return self._clean_text(str(res))

    @staticmethod
    def _download_http(url):
        fd, tmp_path = tempfile.mkstemp(suffix="_audio")
        os.close(fd)
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:
                # 检查文件大小
                content_length = resp.headers.get('Content-Length')
                if content_length and int(content_length) > MAX_CONTENT_LENGTH:
                    os.unlink(tmp_path)
                    raise ValueError("文件大小超过限制: %dMB" % (MAX_CONTENT_LENGTH // 1024 // 1024))
                with open(tmp_path, "wb") as f:
                    shutil.copyfileobj(resp, f)
        except Exception:
            os.unlink(tmp_path)
            raise
        return tmp_path

    async def get_audio_content(self, audio_path):
        tmp_path = None
        try:
            loop = asyncio.get_running_loop()
            if audio_path.startswith(("http://", "https://")):
                real_path = await asyncio.wait_for(
                    loop.run_in_executor(None, self._download_http, audio_path),
                    timeout=DOWNLOAD_TIMEOUT
                )
                tmp_path = real_path
            else:
                real_path = audio_path
            # 检查本地文件大小
            if os.path.getsize(real_path) > MAX_CONTENT_LENGTH:
                raise ValueError("文件大小超过限制: %dMB" % (MAX_CONTENT_LENGTH // 1024 // 1024))
            text = await asyncio.wait_for(
                loop.run_in_executor(None, self._generate_audio, real_path),
                timeout=INFERENCE_TIMEOUT
            )
            return text
        except asyncio.TimeoutError:
            raise TimeoutError("推理超时，请检查文件或降低并发")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)


class PPOCR:
    _instance = None
    _lock = threading.Lock()
    _initialized = False

    def __init__(self):
        if not self.__class__._initialized:
            with self.__class__._lock:
                if not self.__class__._initialized:
                    self.__class__._initialized = True
                    from paddleocr import PaddleOCR
                    if getattr(sys, 'frozen', False):
                        base_dir = os.path.dirname(os.path.abspath(sys.executable))
                    else:
                        base_dir = os.path.dirname(os.path.abspath(__file__))
                    ocr_model_dir = os.path.join(base_dir, "model", "paddleocr")
                    self.ocr = PaddleOCR(
                        use_angle_cls=True,
                        lang='ch',
                        use_pdserving=False,
                        det_model_dir=os.path.join(ocr_model_dir, 'det', 'ch_PP-OCRv4_det_infer'),
                        rec_model_dir=os.path.join(ocr_model_dir, 'rec', 'ch_PP-OCRv4_rec_infer'),
                        cls_model_dir=os.path.join(ocr_model_dir, 'cls', 'ch_ppocr_mobile_v2.0_cls_infer'),
                        show_log=False
                    )
                    print("✓ OCR 模型加载完成 (PaddleOCR PP-OCRv4)")

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
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:
                # 检查文件大小
                content_length = resp.headers.get('Content-Length')
                if content_length and int(content_length) > MAX_CONTENT_LENGTH:
                    os.unlink(tmp_path)
                    raise ValueError("文件大小超过限制: %dMB" % (MAX_CONTENT_LENGTH // 1024 // 1024))
                with open(tmp_path, "wb") as f:
                    shutil.copyfileobj(resp, f)
        except Exception:
            os.unlink(tmp_path)
            raise
        return tmp_path

    def _generate_text(self, image_path):
        if not os.path.exists(image_path):
            raise FileNotFoundError("文件不存在: %s" % image_path)
        result = self.ocr.ocr(image_path)
        if result is None or len(result) == 0:
            return ""
        texts = []
        for line in result:
            if line:
                for item in line:
                    if isinstance(item, list) and len(item) == 2:
                        text = item[1]
                        if isinstance(text, tuple):
                            texts.append(text[0])
                        else:
                            texts.append(text)
        return "\n".join(texts)

    async def get_text_content(self, image_path):
        tmp_path = None
        try:
            loop = asyncio.get_running_loop()
            if image_path.startswith(("http://", "https://")):
                real_path = await asyncio.wait_for(
                    loop.run_in_executor(None, self._download_http, image_path),
                    timeout=DOWNLOAD_TIMEOUT
                )
                tmp_path = real_path
            else:
                real_path = image_path
            # 检查本地文件大小
            if os.path.getsize(real_path) > MAX_CONTENT_LENGTH:
                raise ValueError("文件大小超过限制: %dMB" % (MAX_CONTENT_LENGTH // 1024 // 1024))
            text = await asyncio.wait_for(
                loop.run_in_executor(None, self._generate_text, real_path),
                timeout=INFERENCE_TIMEOUT
            )
            return text
        except asyncio.TimeoutError:
            raise TimeoutError("推理超时，请检查文件或降低并发")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)


class Handler(BaseHTTPRequestHandler):

    def do_POST(self):
        if self.path == '/funasr/identify':
            try:
                start_time = time.time()
                length = int(self.headers.get('Content-Length', 0))
                # 检查请求体大小
                if length > MAX_CONTENT_LENGTH:
                    self._json(413, {'code': 413, 'message': '请求体过大，最大 %dMB' % (MAX_CONTENT_LENGTH // 1024 // 1024), 'data': None})
                    return
                body = json.loads(self.rfile.read(length).decode('utf-8'))
                filepath = body.get('filepath')
                if not filepath:
                    self._json(400, {'code': 400, 'message': '缺少 filepath 参数', 'data': None})
                    return
                text = asyncio.run(FunASR().get_audio_content(filepath))
                duration = time.time() - start_time
                self._json(200, {'code': 200, 'message': '识别成功', 'data': text, 'duration': duration})
            except TimeoutError as e:
                self._json(408, {'code': 408, 'message': str(e), 'data': None})
            except ValueError as e:
                self._json(400, {'code': 400, 'message': str(e), 'data': None})
            except FileNotFoundError as e:
                self._json(400, {'code': 400, 'message': str(e), 'data': None})
            except Exception as e:
                print(e)
                self._json(400, {'code': 400, 'message': '当前系统繁忙，请稍后重试', 'data': None})
        elif self.path == '/ocr/identify':
            try:
                start_time = time.time()
                length = int(self.headers.get('Content-Length', 0))
                # 检查请求体大小
                if length > MAX_CONTENT_LENGTH:
                    self._json(413, {'code': 413, 'message': '请求体过大，最大 %dMB' % (MAX_CONTENT_LENGTH // 1024 // 1024), 'data': None})
                    return
                body = json.loads(self.rfile.read(length).decode('utf-8'))
                filepath = body.get('filepath')
                if not filepath:
                    self._json(400, {'code': 400, 'message': '缺少 filepath 参数', 'data': None})
                    return
                text = asyncio.run(PPOCR().get_text_content(filepath))
                duration = time.time() - start_time
                self._json(200, {'code': 200, 'message': '识别成功', 'data': text, 'duration': duration})
            except TimeoutError as e:
                self._json(408, {'code': 408, 'message': str(e), 'data': None})
            except ValueError as e:
                self._json(400, {'code': 400, 'message': str(e), 'data': None})
            except FileNotFoundError as e:
                self._json(400, {'code': 400, 'message': str(e), 'data': None})
            except Exception as e:
                print(e)
                self._json(400, {'code': 400, 'message': '当前系统繁忙，请稍后重试', 'data': None})
        else:
            self._json(404, {'code': 404, 'message': '未找到路由', 'data': None})

    def do_GET(self):
        if self.path == '/funasr/health':
            self._json(200, {'code': 200, 'status': 'ok'})
        elif self.path == '/ocr/health':
            self._json(200, {'code': 200, 'status': 'ok'})
        else:
            self._json(405, {'code': 405, 'message': '仅支持 POST', 'data': None})

    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print("%s - %s" % (self.address_string(), format % args))


if __name__ == '__main__':
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(base_dir, '.env')

    def read_env():
        host, port = None, None
        if os.path.exists(env_file):
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('FUNASR_HOST='):
                        host = line.split('=', 1)[1]
                    elif line.startswith('FUNASR_PORT='):
                        port = int(line.split('=', 1)[1])
        return host, port

    env_host, env_port = read_env()

    parser = argparse.ArgumentParser()
    parser.add_argument('-host', default=env_host or '127.0.0.1', help='绑定IP (默认: 127.0.0.1)')
    parser.add_argument('-port', type=int, default=env_port or 5001, help='监听端口 (默认: 5001)')
    parser.add_argument('-f', type=str, default=None, help='直接识别音频文件或URL，输出文本后退出')
    args = parser.parse_args()

    if args.f:
        # 根据文件扩展名自动识别类型
        AUDIO_EXTS = {'.wav', '.mp3', '.m4a', '.flac', '.ogg', '.aac', '.wma'}
        IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff', '.webp'}

        ext = os.path.splitext(args.f)[1].lower()
        if ext in AUDIO_EXTS:
            service = 'funasr'
        elif ext in IMAGE_EXTS:
            service = 'ocr'
        else:
            print('错误: 不支持的文件类型: %s' % ext, file=sys.stderr)
            print('支持的音频格式: %s' % ', '.join(AUDIO_EXTS), file=sys.stderr)
            print('支持的图片格式: %s' % ', '.join(IMAGE_EXTS), file=sys.stderr)
            sys.exit(1)

        base = 'http://%s:%d' % (args.host, args.port)
        try:
            urllib.request.urlopen(base + '/' + service + '/health', timeout=3)
        except Exception:
            print('错误: 服务未启动，请先执行 funasr -host %s -port %d' % (args.host, args.port), file=sys.stderr)
            sys.exit(1)
        req = json.dumps({'filepath': args.f}).encode('utf-8')
        resp = urllib.request.urlopen(base + '/' + service + '/identify', data=req, timeout=300)
        result = json.loads(resp.read().decode('utf-8'))
        if result['code'] == 200:
            print(result['data'])
        else:
            print('错误: %s' % result['message'], file=sys.stderr)
            sys.exit(1)
    else:
        print('=' * 50)
        print('FunASR 语音识别服务')
        print('  - 语音识别: SenseVoiceSmall')
        print('  - 文字识别: PaddleOCR PP-OCRv4')
        print('=' * 50)
        print('正在加载语音模型...')
        FunASR()
        print('')
        env_host = '127.0.0.1' if args.host == '0.0.0.0' else args.host
        with open(env_file, 'w') as f:
            f.write('FUNASR_HOST=%s\n' % env_host)
            f.write('FUNASR_PORT=%d\n' % args.port)
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        print('服务已启动: http://%s:%d' % (args.host, args.port))
        print('  POST /funasr/identify  - 语音识别')
        print('  POST /ocr/identify     - 文字识别')
        print('  GET  /funasr/health   - ASR 健康检查')
        print('  GET  /ocr/health      - OCR 健康检查')
        print('')
        print('按 Ctrl+C 停止服务')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\n正在停止服务...')
            server.shutdown()
        finally:
            if os.path.exists(env_file):
                os.unlink(env_file)
