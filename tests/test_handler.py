# -*- coding: utf-8 -*-
"""HTTP Handler 单元测试 — 不启动真实 server,直接测方法逻辑。"""
import json
import threading
from unittest.mock import MagicMock, patch
import pytest


class TestCheckAuth:
    """Handler._check_auth 行为"""

    def test_no_key_configured_allows_all(self, no_auth):
        """未配置 _api_key 时,任何请求都允许(开发模式)"""
        from main import Handler
        h = Handler.__new__(Handler)
        h.headers = {}
        assert h._check_auth() is True

        # 即使带错误 header 也通过(因为 _api_key 是 None)
        h.headers = {'X-API-Key': 'wrong'}
        assert h._check_auth() is True

    def test_correct_key_allowed(self, with_auth):
        from main import Handler
        h = Handler.__new__(Handler)
        h.headers = {'X-API-Key': 'test-secret-123'}
        assert h._check_auth() is True

    def test_wrong_key_rejected(self, with_auth):
        from main import Handler
        h = Handler.__new__(Handler)
        h.headers = {'X-API-Key': 'wrong-key'}
        assert h._check_auth() is False

    def test_missing_header_rejected(self, with_auth):
        from main import Handler
        h = Handler.__new__(Handler)
        h.headers = {}
        assert h._check_auth() is False


class TestDoPostRouting:
    """do_POST 路由逻辑(认证 + 404 + 503)"""

    def test_unknown_route_returns_404(self, no_auth, clean_pools):
        from main import Handler
        h = Handler.__new__(Handler)
        h.path = '/unknown/identify'
        h._json = MagicMock()
        h._handle_request = MagicMock()

        h.do_POST()

        h._json.assert_called_once()
        args = h._json.call_args[0]
        # (status, body)
        assert args[0] == 404
        assert args[1]['code'] == 404
        h._handle_request.assert_not_called()

    def test_pool_not_initialized_returns_503(self, no_auth, clean_pools):
        from main import Handler, ROUTES
        h = Handler.__new__(Handler)
        h.path = '/ocr/identify'  # 在 ROUTES 里,但 pools 空
        h._json = MagicMock()
        h._handle_request = MagicMock()

        h.do_POST()

        h._json.assert_called_once()
        assert h._json.call_args[0][0] == 503
        h._handle_request.assert_not_called()

    def test_auth_failure_returns_401(self, with_auth, clean_pools):
        """认证失败时,即使路由正确也直接 401,不进入路由处理"""
        from main import Handler
        h = Handler.__new__(Handler)
        h.path = '/ocr/identify'
        h.headers = {}  # 缺 API key
        h._json = MagicMock()
        h.send_response = MagicMock()
        h.send_header = MagicMock()
        h.end_headers = MagicMock()
        h.wfile = MagicMock()
        h._handle_request = MagicMock()

        h.do_POST()

        h._handle_request.assert_not_called()  # 不进入路由
        # WWW-Authenticate header 应设置
        auth_header = [c for c in h.send_header.call_args_list
                       if c[0][0] == 'WWW-Authenticate']
        assert auth_header, "Should set WWW-Authenticate header"
        assert auth_header[0][0][1] == 'X-API-Key'


class TestDoGetRouting:
    """do_GET 路由:health / metrics / 405"""

    def test_health_when_alive_worker_returns_200(self, no_auth, clean_pools):
        """health 端点:idle worker 存在时 200"""
        from main import Handler, pools, ElasticProcessPool
        # 注入一个 fake pool
        fake_pool = MagicMock()
        fake_pool.stats.return_value = {'alive': 1, 'idle': 1, 'model_type': 'ocr'}
        pools['ocr'] = fake_pool

        h = Handler.__new__(Handler)
        h.path = '/ocr/health'
        h._json = MagicMock()

        h.do_GET()
        h._json.assert_called_once()
        assert h._json.call_args[0][0] == 200

    def test_health_when_no_idle_returns_503(self, no_auth, clean_pools):
        """health:idle=0 时返 503(K8s readiness 会摘流量)"""
        from main import Handler, pools
        fake_pool = MagicMock()
        fake_pool.stats.return_value = {'alive': 1, 'idle': 0, 'model_type': 'ocr'}
        pools['ocr'] = fake_pool

        h = Handler.__new__(Handler)
        h.path = '/ocr/health'
        h._json = MagicMock()

        h.do_GET()
        h._json.assert_called_once()
        assert h._json.call_args[0][0] == 503

    def test_livez_always_200(self, no_auth, clean_pools):
        """/livez 永远 200(K8s liveness probe)"""
        from main import Handler
        h = Handler.__new__(Handler)
        h.path = '/livez'
        h._json = MagicMock()

        h.do_GET()
        h._json.assert_called_once()
        assert h._json.call_args[0][0] == 200
        assert h._json.call_args[0][1]['status'] == 'alive'

    def test_metrics_requires_auth(self, with_auth, clean_pools):
        """/metrics 无 key 时 401"""
        from main import Handler, pools
        h = Handler.__new__(Handler)
        h.path = '/metrics'
        h.headers = {}  # 无 X-API-Key
        h._json = MagicMock()

        h.do_GET()
        h._json.assert_called_once()
        assert h._json.call_args[0][0] == 401

    def test_metrics_with_auth_prometheus_format(self, with_auth, clean_pools):
        """有 key 时 /metrics 输出 Prometheus 文本格式"""
        from main import Handler, pools
        fake_pool = MagicMock()
        fake_pool.stats.return_value = {
            'model_type': 'ocr', 'alive': 1, 'max': 4, 'min': 1,
            'in_flight': 0, 'idle': 1, 'busy': 0, 'loading': 0,
            'dead': 0, 'scale_events': 0,
        }
        pools['ocr'] = fake_pool
        h = Handler.__new__(Handler)
        h.path = '/metrics'
        h.headers = {'X-API-Key': 'test-secret-123'}
        # mock send_response 等
        h.send_response = MagicMock()
        h.send_header = MagicMock()
        h.end_headers = MagicMock()
        h.wfile = MagicMock()

        h.do_GET()
        h.send_response.assert_called_once_with(200)
        # body 应包含 Prometheus 标签
        body = h.wfile.write.call_args[0][0].decode('utf-8')
        assert 'funasr_pool_alive{model="ocr"} 1' in body
        assert 'funasr_pool_scale_events{model="ocr"} 0' in body

    def test_unknown_get_returns_405(self, no_auth, clean_pools):
        from main import Handler
        h = Handler.__new__(Handler)
        h.path = '/random'
        h._json = MagicMock()

        h.do_GET()
        h._json.assert_called_once()
        assert h._json.call_args[0][0] == 405
