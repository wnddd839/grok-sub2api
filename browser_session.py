# -*- coding: utf-8 -*-
"""浏览器会话管理（线程本地 browser/page）。"""
from __future__ import annotations

import gc
import os
import threading
import time
from typing import Callable, Optional, Tuple

from DrissionPage import Chromium, ChromiumOptions

_tls = threading.local()
_get_proxy: Optional[Callable[[], dict]] = None
_is_debug: Optional[Callable[[], bool]] = None
_extension_path: str = ""


def configure(get_proxies=None, is_debug=None, extension_path=""):
    global _get_proxy, _is_debug, _extension_path
    _get_proxy = get_proxies
    _is_debug = is_debug
    _extension_path = extension_path or ""


def _proxies() -> dict:
    if _get_proxy:
        return _get_proxy() or {}
    return {}


def _debug() -> bool:
    return bool(_is_debug()) if _is_debug else False


def active_browser():
    return getattr(_tls, "browser", None)


def active_page():
    return getattr(_tls, "page", None)


def set_browser_session(browser_obj=None, page_obj=None):
    _tls.browser = browser_obj
    _tls.page = page_obj


class _SessionProxy:
    __slots__ = ("_key",)

    def __init__(self, key):
        self._key = key

    def _obj(self):
        return getattr(_tls, self._key, None)

    def __bool__(self):
        return self._obj() is not None

    def __eq__(self, other):
        return self._obj() is other

    def __ne__(self, other):
        return self._obj() is not other

    def __getattr__(self, name):
        obj = self._obj()
        if obj is None:
            raise AttributeError(f"{self._key} is not started")
        return getattr(obj, name)


browser = _SessionProxy("browser")
page = _SessionProxy("page")


def create_browser_options():
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    if _extension_path and os.path.exists(_extension_path):
        options.add_extension(_extension_path)
    return options


def start_browser(log_callback=None) -> Tuple[object, object]:
    last_exc = None
    for attempt in range(1, 5):
        try:
            browser_obj = Chromium(create_browser_options())
            tabs = browser_obj.get_tabs()
            page_obj = tabs[-1] if tabs else browser_obj.new_tab()
            set_browser_session(browser_obj, page_obj)
            if log_callback and getattr(browser_obj, "user_data_path", None):
                log_callback(f"[Debug] 当前浏览器资料目录: {browser_obj.user_data_path}")
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return browser_obj, page_obj
        except Exception as exc:
            last_exc = exc
            if log_callback:
                log_callback(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
            try:
                cur = active_browser()
                if cur is not None:
                    cur.quit(del_data=True)
            except Exception:
                pass
            set_browser_session(None, None)
            time.sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser(force=False):
    if _debug() and not force:
        return
    current = active_browser()
    set_browser_session(None, None)
    if current is None:
        return
    try:
        current.quit(del_data=True)
    except BaseException:
        pass


def restart_browser(log_callback=None):
    stop_browser(force=True)
    return start_browser(log_callback=log_callback)


def cleanup_runtime_memory(log_callback=None, reason="定期清理"):
    try:
        if _debug():
            if log_callback:
                log_callback(f"[*] 调试模式：保留浏览器（{reason}）")
            collected = gc.collect()
            if log_callback:
                log_callback(f"[*] Python GC 已回收对象数: {collected}")
            return
        if log_callback:
            log_callback(f"[*] {reason}: 关闭浏览器并清理内存")
        stop_browser(force=True)
        collected = gc.collect()
        if log_callback:
            log_callback(f"[*] Python GC 已回收对象数: {collected}")
    except BaseException:
        try:
            if not _debug():
                stop_browser(force=True)
        except BaseException:
            pass


def refresh_active_page():
    if active_browser() is None:
        restart_browser()
    try:
        browser_obj = active_browser()
        tabs = browser_obj.get_tabs()
        page_obj = tabs[-1] if tabs else browser_obj.new_tab()
        set_browser_session(browser_obj, page_obj)
    except Exception:
        restart_browser()
    return page


def extract_cf_clearance_and_ua(log_callback=None, ensure_grok=True):
    """提取 grok.com 域 cf_clearance + UA。"""
    cf_clearance = ""
    user_agent = ""
    try:
        active = refresh_active_page()
        if active is None:
            return "", ""

        def _read_cf_and_ua(page_obj, grok_only=False):
            clearance = ""
            ua_text = ""
            cookies = page_obj.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                    domain = str(item.get("domain", "")).strip().lower()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()
                    domain = str(getattr(item, "domain", "")).strip().lower()
                if name != "cf_clearance" or not value:
                    continue
                if grok_only and "grok.com" not in domain:
                    continue
                if "grok.com" in domain:
                    clearance = value
                    break
                if not clearance and not grok_only:
                    clearance = value
            try:
                ua = page_obj.run_js("return navigator.userAgent;")
                if ua:
                    ua_text = str(ua).strip()
            except Exception:
                pass
            return clearance, ua_text

        def _page_passed_cf(page_obj):
            try:
                title = str(page_obj.run_js("return document.title || '';") or "").lower()
                body = str(
                    page_obj.run_js(
                        "return (document.body && (document.body.innerText||'')) || '';"
                    )
                    or ""
                ).lower()
                if "just a moment" in title or "just a moment" in body[:200]:
                    return False
                if "checking your browser" in body[:300]:
                    return False
                return True
            except Exception:
                return False

        cf_clearance, user_agent = _read_cf_and_ua(active, grok_only=True)
        if ensure_grok and not cf_clearance:
            if log_callback:
                log_callback("[*] 未找到 grok.com 的 cf_clearance，打开 grok.com 过盾...")
            try:
                active.get("https://grok.com/")
                try:
                    active.wait.doc_loaded()
                except Exception:
                    pass
                time.sleep(2)
                for _ in range(20):
                    if _page_passed_cf(active):
                        cf_clearance, user_agent = _read_cf_and_ua(active, grok_only=True)
                        if cf_clearance:
                            break
                    time.sleep(1.0)
                if log_callback:
                    if cf_clearance:
                        log_callback("[*] 已取得 grok.com 的 cf_clearance")
                    else:
                        log_callback(
                            "[!] 打开 grok.com 后仍无有效 cf_clearance（页面可能仍卡在 Just a moment）"
                        )
            except Exception as nav_exc:
                if log_callback:
                    log_callback(f"[Debug] 打开 grok.com 取 cf_clearance 失败: {nav_exc}")
                cf_clearance, user_agent = _read_cf_and_ua(active, grok_only=True)
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 提取 cf_clearance 失败: {exc}")
    return cf_clearance, user_agent
