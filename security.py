# -*- coding: utf-8 -*-
"""安全相关 helper — SSRF 防御、路径白名单、远程文件下载

导出:
- _is_safe_url(url) — 拒绝指向内网/metadata 的 URL
- _NoRedirect — 禁止 HTTP 30x 重定向(handler 防绕开 SSRF)
- _is_safe_path(filepath, allowed_dirs) — 路径白名单校验 + realpath 解析
- download_http_file(url, suffix) — 远程文件下载,带 SSRF 防御

【L1 拆分】从 main.py 抽出,handler.py 等会从这 import。
"""
import ipaddress
import logging
import os
import socket
import shutil
import tempfile
import urllib.request
from urllib.parse import urlparse

# 从 main 导入常量(max 文件大小等),保持单一来源
# 用 try/except 避免 cycle(若 main 反向 import security)
_MAX_CONTENT_LENGTH = 100 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 60

logger = logging.getLogger('funasr.security')


def _is_safe_url(url: str) -> bool:
    """SSRF 防御:拒绝指向内网/metadata 的 URL

    - scheme 仅允许 http/https
    - getaddrinfo 解析所有 IP,任意一个在内网段就拒
    - 常见 metadata 主机名黑名单
    """
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False
        host = p.hostname
        if not host:
            return False
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False
        for info in set(infos):
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        if host.lower() in {'metadata.google.internal', 'metadata',
                            'kubernetes.default.svc', 'localhost'}:
            return False
        return True
    except Exception:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止 HTTP 30x 重定向,防 30x 跳到内网绕开 SSRF 防护"""
    def http_error_301(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_302(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_303(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_307(self, req, fp, code, msg, headers): self._block(headers)
    def http_error_308(self, req, fp, code, msg, headers): self._block(headers)
    def _block(self, headers):
        raise ValueError(f"HTTP redirect not allowed: {headers.get('Location')}")


def _is_safe_path(filepath: str, allowed_dirs: list) -> str:
    """路径白名单防御:
    - os.path.realpath 解析符号链接和 ..
    - 必须在 allowed_dirs 列表内的某个目录下(允许该目录本身或其子文件)
    - 返回规范化后的绝对路径
    """
    real = os.path.realpath(filepath)
    for allowed in allowed_dirs:
        if real == allowed or real.startswith(allowed + os.sep):
            return real
    raise ValueError(f"Path not allowed (不在白名单目录): {filepath}")


def download_http_file(url: str, suffix: str) -> str:
    """远程下载文件到临时目录,带 SSRF 防御"""
    if not _is_safe_url(url):
        raise ValueError(f"URL not allowed: {url}")
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        # 用自定义 opener(带 NoRedirect),仅本函数作用域
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(url, timeout=_DOWNLOAD_TIMEOUT) as resp:
            content_length = resp.headers.get('Content-Length')
            if content_length and int(content_length) > _MAX_CONTENT_LENGTH:
                os.unlink(tmp_path)
                raise ValueError("文件大小超过限制")
            with open(tmp_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        return tmp_path
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
