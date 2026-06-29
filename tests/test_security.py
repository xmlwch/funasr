# -*- coding: utf-8 -*-
"""安全相关纯逻辑测试:SSRF + 路径白名单。不依赖 model 或 worker。"""
import os
import sys
import pytest


class TestIsSafeUrl:
    """_is_safe_url 拒绝指向内网/metadata 的 URL"""

    def test_https_public_url_allowed(self):
        from security import _is_safe_url
        assert _is_safe_url('https://8.8.8.8/test.png') is True

    def test_http_public_url_allowed(self):
        from security import _is_safe_url
        assert _is_safe_url('http://1.1.1.1/test.png') is True

    def test_localhost_rejected(self):
        from security import _is_safe_url
        assert _is_safe_url('http://127.0.0.1/test.png') is False
        assert _is_safe_url('http://127.0.0.1:8080/admin') is False

    def test_private_ip_rejected(self):
        from security import _is_safe_url
        assert _is_safe_url('http://10.0.0.5/test.png') is False
        assert _is_safe_url('http://192.168.1.1/admin.png') is False
        assert _is_safe_url('http://172.16.0.1/test.png') is False

    def test_link_local_metadata_rejected(self):
        from security import _is_safe_url
        assert _is_safe_url('http://169.254.169.254/latest/meta-data/') is False
        assert _is_safe_url('http://169.254.0.1/x.png') is False

    def test_metadata_hostname_blacklist(self):
        from security import _is_safe_url
        assert _is_safe_url('http://metadata.google.internal/x.png') is False
        assert _is_safe_url('http://metadata/x.png') is False
        assert _is_safe_url('http://localhost/x.png') is False

    def test_file_protocol_rejected(self):
        from security import _is_safe_url
        assert _is_safe_url('file:///etc/passwd.png') is False
        assert _is_safe_url('file://localhost/etc/passwd.png') is False

    def test_ftp_protocol_rejected(self):
        from security import _is_safe_url
        assert _is_safe_url('ftp://1.2.3.4/test.png') is False
        assert _is_safe_url('gopher://1.2.3.4/') is False

    def test_invalid_url_returns_false(self):
        from security import _is_safe_url
        assert _is_safe_url('not-a-url') is False
        assert _is_safe_url('') is False
        assert _is_safe_url('http://') is False

    def test_multicast_and_reserved_rejected(self):
        from security import _is_safe_url
        assert _is_safe_url('http://224.0.0.1/x.png') is False
        assert _is_safe_url('http://240.0.0.1/x.png') is False


class TestIsSafePath:
    """_is_safe_path 路径白名单校验"""

    def test_allowed_path_accepted(self, tmp_path):
        from security import _is_safe_path
        from main import _ALLOWED_DIRS
        allowed = str(tmp_path)
        _ALLOWED_DIRS.clear()
        _ALLOWED_DIRS.append(allowed)
        f = tmp_path / "test.png"
        f.write_text("x")
        assert _is_safe_path(str(f), _ALLOWED_DIRS) == str(f)

    def test_disallowed_path_rejected(self, tmp_path):
        from security import _is_safe_path
        from main import _ALLOWED_DIRS
        allowed = str(tmp_path)
        _ALLOWED_DIRS.clear()
        _ALLOWED_DIRS.append(allowed)
        with pytest.raises(ValueError, match="Path not allowed"):
            _is_safe_path('/etc/passwd.png', _ALLOWED_DIRS)

    def test_symlink_to_outside_rejected(self, tmp_path):
        from security import _is_safe_path
        from main import _ALLOWED_DIRS
        allowed = str(tmp_path)
        _ALLOWED_DIRS.clear()
        _ALLOWED_DIRS.append(allowed)
        link = tmp_path / "sneaky.png"
        target = "/etc/passwd"
        try:
            link.symlink_to(target)
            with pytest.raises(ValueError, match="Path not allowed"):
                _is_safe_path(str(link), _ALLOWED_DIRS)
        except (OSError, NotImplementedError):
            pytest.skip("symlink not supported on this platform")

    def test_parent_traversal_rejected(self, tmp_path):
        from security import _is_safe_path
        from main import _ALLOWED_DIRS
        allowed = str(tmp_path)
        _ALLOWED_DIRS.clear()
        _ALLOWED_DIRS.append(allowed)
        with pytest.raises(ValueError, match="Path not allowed"):
            _is_safe_path(str(tmp_path / ".." / ".." / "etc" / "passwd.png"),
                          _ALLOWED_DIRS)

    def test_subdirectory_allowed(self, tmp_path):
        from security import _is_safe_path
        from main import _ALLOWED_DIRS
        allowed = str(tmp_path)
        _ALLOWED_DIRS.clear()
        _ALLOWED_DIRS.append(allowed)
        sub = tmp_path / "subdir"
        sub.mkdir()
        f = sub / "test.png"
        f.write_text("x")
        assert _is_safe_path(str(f), _ALLOWED_DIRS) == str(f)
