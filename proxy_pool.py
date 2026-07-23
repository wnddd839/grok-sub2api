#!/usr/bin/env python3
"""注册机代理池：一号一出口 / 每 IP 限 N 个成功号后轮换。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit


LogFn = Callable[[str], None]


def normalize_proxy_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text or text.startswith("#"):
        return ""
    if "://" not in text:
        text = "http://" + text
    return text


def mask_proxy_url(raw: str) -> str:
    """日志用：隐藏密码。"""
    text = normalize_proxy_url(raw)
    if not text:
        return "(direct)"
    try:
        parts = urlsplit(text)
        if not parts.hostname:
            return text
        user = parts.username or ""
        host = parts.hostname
        port = f":{parts.port}" if parts.port else ""
        auth = f"{user}:***@" if user else ""
        return f"{parts.scheme}://{auth}{host}{port}"
    except Exception:
        return text.split("@")[-1] if "@" in text else text


def load_proxy_list(
    pool: list | None = None,
    pool_file: str = "",
    single: str = "",
) -> list[str]:
    """合并 config.proxy_pool / 文件 / 单条 proxy，去重保序。"""
    found: list[str] = []
    seen: set[str] = set()

    def _add(item: str):
        url = normalize_proxy_url(item)
        if not url or url in seen:
            return
        seen.add(url)
        found.append(url)

    if isinstance(pool, list):
        for item in pool:
            _add(str(item))
    path = str(pool_file or "").strip()
    if path:
        p = Path(path)
        if p.is_file():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                _add(line)
    _add(single)
    return found


@dataclass
class ProxyLease:
    url: str
    success_count: int = 0
    fail_count: int = 0
    retired: bool = False


@dataclass
class ProxyRotator:
    """线程安全代理轮换。

    accounts_per_ip: 同一出口累计「注册成功」达到该数后自动换下一个。
    失败可 retire 当前出口（可选）。
    """

    proxies: list[str]
    accounts_per_ip: int = 1
    rotate_on_fail: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _leases: dict[str, ProxyLease] = field(default_factory=dict, repr=False)
    _order: list[str] = field(default_factory=list, repr=False)
    _cursor: int = 0

    def __post_init__(self):
        self.accounts_per_ip = max(1, int(self.accounts_per_ip or 1))
        urls = [normalize_proxy_url(x) for x in self.proxies]
        urls = [x for x in urls if x]
        self._order = list(dict.fromkeys(urls))
        self._leases = {u: ProxyLease(url=u) for u in self._order}
        self._cursor = 0

    @property
    def size(self) -> int:
        return len(self._order)

    def available_count(self) -> int:
        with self._lock:
            return sum(1 for u in self._order if not self._leases[u].retired)

    def acquire(self, current: str = "") -> str | None:
        """取一个未退役出口；若 current 仍可用且未达上限则继续粘住。"""
        with self._lock:
            cur = normalize_proxy_url(current)
            if cur and cur in self._leases:
                lease = self._leases[cur]
                if (
                    not lease.retired
                    and lease.success_count < self.accounts_per_ip
                ):
                    return cur
            n = len(self._order)
            if n == 0:
                return None
            for _ in range(n):
                url = self._order[self._cursor % n]
                self._cursor += 1
                lease = self._leases[url]
                if not lease.retired and lease.success_count < self.accounts_per_ip:
                    return url
            return None

    def record_success(self, proxy: str) -> bool:
        """记一次成功。返回 True 表示该出口已达上限，下次应换。"""
        url = normalize_proxy_url(proxy)
        if not url:
            return False
        with self._lock:
            lease = self._leases.get(url)
            if lease is None:
                lease = ProxyLease(url=url)
                self._leases[url] = lease
                self._order.append(url)
            lease.success_count += 1
            if lease.success_count >= self.accounts_per_ip:
                lease.retired = True
                return True
            return False

    def record_fail(self, proxy: str, retire: bool | None = None) -> None:
        url = normalize_proxy_url(proxy)
        if not url:
            return
        with self._lock:
            lease = self._leases.get(url)
            if lease is None:
                return
            lease.fail_count += 1
            if retire is None:
                retire = self.rotate_on_fail
            if retire:
                lease.retired = True

    def summary(self) -> str:
        with self._lock:
            alive = sum(1 for u in self._order if not self._leases[u].retired)
            used = sum(self._leases[u].success_count for u in self._order)
            return f"池内 {len(self._order)} 条 | 仍可用 {alive} | 本轮成功占用 {used}"
