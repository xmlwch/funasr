# -*- coding: utf-8 -*-
"""共享 pytest fixtures — 主要用于隔离 main 模块的全局状态。"""
import os
import sys
import pytest

# 注入项目根目录到 sys.path,让 main.py / worker.py / _paths.py 可 import
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def allowed_dirs():
    """测试用的白名单目录(绝不能让路径穿越测试污染真实目录)"""
    return [
        os.path.realpath(os.path.expanduser('~/uploads')),
        os.path.realpath(os.path.join(PROJECT_ROOT, 'tests', 'tmp')),
    ]


@pytest.fixture
def clean_pools():
    """每个测试前清空 main 模块的全局 pools dict,避免上次测试残留"""
    import main
    main.pools.clear()
    main._ALLOWED_DIRS.clear()
    yield
    main.pools.clear()
    main._ALLOWED_DIRS.clear()


@pytest.fixture
def no_auth(monkeypatch):
    """默认禁用 API key(让 _check_auth 永远通过),测试不依赖认证逻辑"""
    from main import Handler
    monkeypatch.setattr(Handler, '_api_key', None)
    yield


@pytest.fixture
def with_auth(monkeypatch):
    """启用 API key 认证"""
    from main import Handler
    monkeypatch.setattr(Handler, '_api_key', 'test-secret-123')
    yield
