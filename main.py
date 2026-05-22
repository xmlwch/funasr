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
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from funasr_onnx import SenseVoiceSmall


_TAG_PATTERN = re.compile(r'<\|[^|]+\|>')


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
                    print("语音模型初始化成功")

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    def _clean_text(text):
        text = _TAG_PATTERN.sub('', text).strip()
        return text

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
            with urllib.request.urlopen(url, timeout=30) as resp, open(tmp_path, "wb") as f:
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
                real_path = await loop.run_in_executor(None, self._download_http, audio_path)
                tmp_path = real_path
            else:
                real_path = audio_path
            text = await loop.run_in_executor(None, self._generate_audio, real_path)
            return text
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)


class Handler(BaseHTTPRequestHandler):

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
        else:
            self._json(404, {'code': 404, 'message': '未找到路由', 'data': None})

    def do_GET(self):
        if self.path == '/funasr/health':
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
        base = 'http://%s:%d' % (args.host, args.port)
        try:
            urllib.request.urlopen(base + '/funasr/health', timeout=3)
        except Exception:
            print('错误: 服务未启动，请先执行 funasr -host %s -port %d' % (args.host, args.port), file=sys.stderr)
            sys.exit(1)
        req = json.dumps({'filepath': args.f}).encode('utf-8')
        resp = urllib.request.urlopen(base + '/funasr/identify', data=req, timeout=300)
        result = json.loads(resp.read().decode('utf-8'))
        if result['code'] == 200:
            print(result['data'])
        else:
            print('错误: %s' % result['message'], file=sys.stderr)
            sys.exit(1)
    else:
        print('正在加载语音模型...')
        FunASR()
        env_host = '127.0.0.1' if args.host == '0.0.0.0' else args.host
        with open(env_file, 'w') as f:
            f.write('FUNASR_HOST=%s\n' % env_host)
            f.write('FUNASR_PORT=%d\n' % args.port)
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        print('FunASR 服务已启动: http://%s:%d' % (args.host, args.port))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\n服务已停止')
            server.shutdown()
        finally:
            if os.path.exists(env_file):
                os.unlink(env_file)
