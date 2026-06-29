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

# 模块本地常量 — 与 main 解耦(避免循环依赖)
_MAX_CONTENT_LENGTH = 100 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 60

# hostname 黑名单(metadata 等即使信任列表也不能放行)
_HOSTNAME_BLACKLIST = frozenset({
    'metadata.google.internal', 'metadata',
    'kubernetes.default.svc', 'localhost',
})

logger = logging.getLogger('funasr.security')


def _parse_trusted_hosts(spec: str) -> dict:
    """解析 -allowed-internal-hosts 参数为 {hostnames, ip_literals, cidrs}

    支持格式(逗号分隔):
      hostname    localhost / internal.api.local
      IP 字面量  127.0.0.1 / 192.168.1.100
      CIDR        10.0.0.0/8 / 192.168.0.0/16

    Returns:
        {
            'hostnames': set[str],      # 精确匹配 hostname
            'ip_literals': set[str],    # 精确匹配 IP 字面量
            'cidrs': list[IPv4Network|IPv6Network],
        }
    """
    hostnames = set()
    ip_literals = set()
    cidrs = []
    if not spec:
        return {'hostnames': hostnames, 'ip_literals': ip_literals, 'cidrs': cidrs}
    for raw in spec.split(','):
        item = raw.strip().lower()
        if not item:
            continue
        if '/' in item:
            # 含 / 的当 CIDR — 解析失败直接 skip,不 fallback 到 hostname(否则 confusing)
            try:
                cidrs.append(ipaddress.ip_network(item, strict=False))
                continue
            except ValueError:
                logger.warning("-allowed-internal-hosts CIDR 解析失败: %s", item)
                continue
        try:
            ip = ipaddress.ip_address(item)
            ip_literals.add(str(ip))
            continue
        except ValueError:
            pass
        # 当作 hostname
        hostnames.add(item)
    return {'hostnames': hostnames, 'ip_literals': ip_literals, 'cidrs': cidrs}


def _is_safe_url(url: str, allowed_hosts: dict = None) -> bool:
    """SSRF 防御 + 可选信任列表 bypass

    默认严格:拒绝指向内网/metadata 的 URL。
    allowed_hosts 非空时,hostnames/ip_literals/CIDRs 命中可绕过 IP 段检查;
    但 hostname 黑名单(metadata.google.internal 等)始终拒。

    格式:
      - allowed-internal-hosts 项 = hostname 字面 / IP 字面 / CIDR
    """
    if allowed_hosts is None:
        allowed_hosts = {'hostnames': set(), 'ip_literals': set(), 'cidrs': []}
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False
        host = p.hostname
        if not host:
            return False
        host_lower = host.lower()

        # 1) hostname 黑名单始终拒(信任列表也无法 bypass 元数据接口)
        if host_lower in _HOSTNAME_BLACKLIST:
            return False

        # 2) hostname 在信任列表 → 通过
        if host_lower in allowed_hosts['hostnames']:
            return True

        # 3) 解析所有 IP,逐个检查
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False
        for info in set(infos):
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)

            # 信任 IP 字面量
            if ip_str in allowed_hosts['ip_literals']:
                continue
            # 信任 CIDR
            in_cidr = False
            for net in allowed_hosts['cidrs']:
                if ip in net:
                    in_cidr = True
                    break
            if in_cidr:
                continue

            # 标准 SSRF 检查
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
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


def download_http_file(url: str, suffix: str, allowed_hosts: dict = None) -> str:
    """远程下载文件到临时目录,带 SSRF 防御

    allowed_hosts (None 或 dict) 透传给 _is_safe_url
    """
    if not _is_safe_url(url, allowed_hosts):
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
