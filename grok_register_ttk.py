#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - TTK GUI 版本
整合 DrissionPage_example.py, openai_register.py, batch_open_nsfw.py
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import datetime
import time
import os
import sys
import signal
import gc
import queue
import secrets
import struct
import random
import re
import string
import json

os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
from curl_cffi import requests

# SSO → CLIProxyAPI(CPA) 扁平格式转换（复用 sso_to_auth_json 的授权码流程 + 写入器）
import sso_to_auth_json as _s2cpa
from email_providers import cloudflare as cloudflare_provider
from email_providers import cloudmail as cloudmail_provider
from email_providers import duckmail as duckmail_provider
from email_providers import mailnest as mailnest_provider
from email_providers import yyds as yyds_provider
from email_providers.common import extract_verification_code as _extract_code
from email_providers.common import generate_username as _generate_username
from email_providers.common import pick_list_payload as _pick_list


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
MEMORY_CLEANUP_INTERVAL = 5

UI_BG = "#242424"
UI_PANEL_BG = "#2b2b2b"
UI_FG = "#f2f2f2"
UI_MUTED_FG = "#b8b8b8"
UI_ENTRY_BG = "#333333"
UI_BUTTON_BG = "#3a3a3a"
UI_ACTIVE_BG = "#4a6078"

DEFAULT_CONFIG = {
    "email_provider": "cloudflare",
    "duckmail_api_key": "",
    "duckmail_api_base": "https://api.duckmail.sbs",
    "defaultDomains": "",
    "cloudmail_url": "",
    "cloudmail_admin_email": "",
    "cloudmail_password": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "none",
    "cloudflare_custom_auth": "",
    "cloudflare_path_domains": "/api/domains",
    "cloudflare_path_accounts": "/api/new_address",
    "cloudflare_path_token": "/api/token",
    "cloudflare_path_messages": "/api/mails",
    "proxy": "http://127.0.0.1:7890",
    "enable_nsfw": True,
    "register_count": 1,
    "register_workers": 1,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    # CLIProxyAPI(CPA) 直出：注册拿到 SSO 后自动走授权码流程换 token 并写成 CPA 扁平格式
    "cpa_auto_add": False,
    "cpa_auth_dir": "",
    # 远程 CPA：通过 Management API POST /v0/management/auth-files 上传
    "cpa_remote_url": "",
    "cpa_management_key": "",
    "mailnest_api_key": "",
    "mailnest_project_code": "x-ai001",
    # YYDS：留空自动选已验证域名；填写则固定该域名
    "yyds_default_domain": "",
    # Sub2API：注册成功后按批次写出导入包 / 可选远程创建账号
    "sub2api_auto_add": False,
    "sub2api_dir": "",
    "sub2api_url": "",
    "sub2api_token": "",
    "sub2api_batch_size": 20,
    "sub2api_verify": True,
    "sub2api_verify_workers": 3,
}

config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0
_sub2api_batch_writer = None
_sub2api_io_lock = threading.Lock()
_sub2api_executor = None
_sub2api_futures = []
_sub2api_futures_lock = threading.Lock()


class RegistrationCancelled(Exception):
    pass


class AccountRetryNeeded(Exception):
    pass


class EmailDomainRejected(Exception):
    """xAI 拒绝当前邮箱域名（如公共临时域被拉黑）。"""

    def __init__(self, email="", message=""):
        self.email = email or ""
        self.message = message or "邮箱域名已被拒绝"
        domain = ""
        if "@" in self.email:
            domain = self.email.split("@", 1)[1]
        detail = self.message
        if domain and domain not in detail:
            detail = f"{detail}（域名: {domain}）"
        if self.email and self.email not in detail:
            detail = f"{detail} | 邮箱: {self.email}"
        super().__init__(detail)



FAIL_DOMAIN = "domain_rejected"
FAIL_CODE = "code_timeout"
FAIL_BROWSER = "browser"
FAIL_CPA = "cpa"
FAIL_VERIFY = "verify"
FAIL_STUCK = "stuck_retry"
FAIL_OTHER = "other"

FAIL_LABELS = {
    FAIL_DOMAIN: "域名拒绝",
    FAIL_CODE: "验证码超时",
    FAIL_BROWSER: "浏览器断开",
    FAIL_CPA: "CPA失败",
    FAIL_VERIFY: "验活失败",
    FAIL_STUCK: "流程卡住",
    FAIL_OTHER: "其它",
}


def classify_failure(exc) -> str:
    if isinstance(exc, EmailDomainRejected):
        return FAIL_DOMAIN
    msg = str(exc or "")
    low = msg.lower()
    if isinstance(exc, AccountRetryNeeded) or "达到最大重试" in msg or "流程卡住" in msg:
        return FAIL_STUCK
    if "未收到验证码" in msg or "验证码阶段失败" in msg or "验证码" in msg and "失败" in msg:
        return FAIL_CODE
    if (
        "浏览器" in msg
        or "page disconnected" in low
        or "与页面的连接已断开" in msg
        or "PageDisconnected" in msg
        or "disconnected" in low
    ):
        return FAIL_BROWSER
    if "[CPA]" in msg or "CPA" in msg and ("失败" in msg or "跳过" in msg):
        return FAIL_CPA
    if "验活" in msg or "换 token" in msg or "verify" in low:
        return FAIL_VERIFY
    return FAIL_OTHER


def empty_fail_stats():
    return {k: 0 for k in FAIL_LABELS}


def format_fail_stats(stats: dict) -> str:
    parts = [f"{FAIL_LABELS.get(k, k)}={stats.get(k, 0)}" for k in FAIL_LABELS if stats.get(k, 0)]
    if not parts:
        return "无分类失败"
    return " | ".join(parts)



def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception:
            config = DEFAULT_CONFIG.copy()
    return config


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")


def ensure_stable_python_runtime():
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        print(
            "[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。"
        )


ensure_stable_python_runtime()
warn_runtime_compatibility()

load_config()

EXTENSION_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "turnstilePatch")
)


DUCKMAIL_API_BASE_DEFAULT = duckmail_provider.API_BASE_DEFAULT


def get_proxies():
    proxy = config.get("proxy", "")
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def get_duckmail_api_base():
    return duckmail_provider.normalize_base(str(config.get("duckmail_api_base", "") or ""))


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")



def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "none") or "none").lower()


def get_cloudflare_custom_auth():
    """全局访问密码（cloudflare_temp_email 的 PASSWORDS）。"""
    return str(config.get("cloudflare_custom_auth", "") or "").strip()


def cloudflare_apply_custom_auth(headers):
    return cloudflare_provider.apply_custom_auth(headers, get_cloudflare_custom_auth())


def get_cloudflare_path(key, default_path):
    return cloudflare_provider.path_from_config(config, key, default_path)


def cloudflare_build_headers(content_type=False):
    return cloudflare_provider.build_headers(
        get_cloudflare_api_key(),
        get_cloudflare_auth_mode(),
        get_cloudflare_custom_auth(),
        content_type=content_type,
    )


def cloudflare_apply_auth_params(params=None):
    return cloudflare_provider.apply_auth_params(
        params, get_cloudflare_api_key(), get_cloudflare_auth_mode()
    )


def cloudflare_next_default_domain():
    global _cf_domain_index
    domains = [x.strip() for x in str(config.get("defaultDomains", "") or "").split(",") if x.strip()]
    domain, _cf_domain_index = cloudflare_provider.next_default_domain(domains, _cf_domain_index)
    return domain


def cloudflare_is_admin_create_path(path):
    return cloudflare_provider.is_admin_create_path(path)


def _pick_list_payload(data):
    return _pick_list(data)


def cloudflare_create_temp_address(api_base):
    return cloudflare_provider.create_temp_address(
        http_post,
        api_base,
        accounts_path=get_cloudflare_path("cloudflare_path_accounts", "/api/new_address"),
        domain=cloudflare_next_default_domain(),
        api_key=get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
        name=generate_username(10),
    )


MAILNEST_API_BASE = mailnest_provider.API_BASE
MAILNEST_DEFAULT_PROJECT_CODE = mailnest_provider.DEFAULT_PROJECT_CODE


def get_mailnest_api_key():
    key = str(config.get("mailnest_api_key", "") or "").strip()
    if not key:
        raise Exception(f"请在配置文件中配置 mailnest_api_key | 注册网址：{MAILNEST_API_BASE}")
    return key


def get_mailnest_project_code():
    code = str(config.get("mailnest_project_code", "") or "").strip()
    return code or MAILNEST_DEFAULT_PROJECT_CODE


def mailnest_buy_email():
    return mailnest_provider.buy_email(http_post, get_mailnest_api_key(), get_mailnest_project_code())


def mailnest_receive_email(email):
    return mailnest_provider.receive_email(http_post, get_mailnest_api_key(), email)


def mailnest_get_code(email, timeout=180, poll_interval=3, log_callback=None, cancel_callback=None):
    return mailnest_provider.wait_for_code(
        http_post,
        get_mailnest_api_key(),
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def get_user_agent():
    return config.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


def _normalize_sso_token(raw_token):
    token = str(raw_token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def _resolve_cpa_proxy():
    """CPA 换 token 用的代理：优先 config.proxy，其次环境变量，最后本机 7890。"""
    proxy = str(config.get("proxy", "") or "").strip()
    if proxy:
        return proxy
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        val = str(os.environ.get(key, "") or "").strip()
        if val:
            return val
    return "http://127.0.0.1:7890"


def begin_sub2api_batch_session(log_callback=None):
    """为本次注册任务启动验活线程池；若开启直出则同时创建分包目录。"""
    global _sub2api_batch_writer, _sub2api_executor, _sub2api_futures
    wait_sub2api_pending(log_callback=log_callback)
    _sub2api_batch_writer = None
    export_enabled = bool(config.get("sub2api_auto_add", False))
    verify_enabled = bool(config.get("sub2api_verify", True))
    out_dir = str(config.get("sub2api_dir", "") or "").strip() if export_enabled else ""
    remote_url = str(config.get("sub2api_url", "") or "").strip() if export_enabled else ""
    # 即便不导出，也需要线程池做换 token / 验活（成功门槛）
    if not export_enabled and not verify_enabled:
        return None
    if export_enabled and not out_dir and not remote_url and not verify_enabled:
        return None
    try:
        batch_size = max(int(config.get("sub2api_batch_size", 20) or 20), 1)
    except (TypeError, ValueError):
        batch_size = 20
    try:
        workers = max(int(config.get("sub2api_verify_workers", 3) or 3), 1)
    except (TypeError, ValueError):
        workers = 3
    try:
        if out_dir:
            _sub2api_batch_writer = _s2cpa.Sub2APIBatchWriter(
                _s2cpa.Path(out_dir),
                batch_size=batch_size,
            )
        from concurrent.futures import ThreadPoolExecutor

        _sub2api_executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="sub2api-verify"
        )
        with _sub2api_futures_lock:
            _sub2api_futures = []
        if log_callback:
            if _sub2api_batch_writer is not None:
                log_callback(
                    f"[Sub2API] 新批次目录: {_sub2api_batch_writer.session_dir} "
                    f"（每包 {batch_size} 个，验活并发 {workers}）"
                )
            elif remote_url:
                log_callback(f"[Sub2API] 远程直推已启用（验活并发 {workers}）")
            else:
                log_callback(f"[Sub2API] 验活线程池已启动（并发 {workers}，成功以验活为准）")
        return _sub2api_batch_writer
    except Exception as exc:
        if log_callback:
            log_callback(f"[Sub2API] 创建批次会话失败: {exc}")
        return None


def wait_sub2api_pending(log_callback=None):
    """等待并行验活/导出任务结束。"""
    global _sub2api_executor, _sub2api_futures
    with _sub2api_futures_lock:
        futures = list(_sub2api_futures)
        _sub2api_futures = []
    if futures:
        if log_callback:
            log_callback(f"[Sub2API] 等待 {len(futures)} 个验活任务完成 ...")
        from concurrent.futures import wait

        wait(futures)
    executor = _sub2api_executor
    _sub2api_executor = None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=False)


def add_sso_to_cpa(raw_token, email="", log_callback=None):
    """SSO → 换 token → 写入本地 CPA auth 目录和/或远程 CPA。

    SSO 本身不是 CPA 认的凭据；必须先换到 access/refresh token，
    再写成 CPA 的 xai-<email>.json（type=xai + cli-chat-proxy base_url + grok-cli headers）。
    实际的 SSO→token 换取流程由 sso_to_auth_json(_s2cpa) 决定（当前为 Device Flow）。

    - 本地：写入 cpa_auth_dir，CPA 监听热加载
    - 远程：POST Management API /v0/management/auth-files（cpa_remote_url + cpa_management_key）
    - cpa_auto_add=false 时跳过转换，仅保留 accounts 文件里的 SSO
    """
    if not config.get("cpa_auto_add", False):
        if log_callback:
            log_callback("[*] 已关闭 SSO→CPA auth，仅保存 SSO（不写 auth）")
        return
    auth_dir = str(config.get("cpa_auth_dir", "") or "").strip()
    remote_url = str(config.get("cpa_remote_url", "") or "").strip()
    management_key = str(config.get("cpa_management_key", "") or "").strip()
    if not auth_dir and not remote_url:
        if log_callback:
            log_callback("[Debug] 已开启 CPA 直出但未配置 cpa_auth_dir 或 cpa_remote_url，跳过")
        return
    if remote_url and not management_key:
        if log_callback:
            log_callback("[Debug] 已配置 cpa_remote_url 但未配置 cpa_management_key，跳过远程上传")
        remote_url = ""
    if not auth_dir and not remote_url:
        return
    sso = _normalize_sso_token(raw_token)
    if not sso:
        return
    proxy = _resolve_cpa_proxy()

    def _cpa_log(message):
        if log_callback:
            # Windows 控制台/部分 UI 回调默认 GBK，emoji 会导致整段 CPA 直出中断
            text = str(message).strip()
            try:
                text.encode("gbk")
            except UnicodeEncodeError:
                text = text.encode("gbk", errors="replace").decode("gbk")
            log_callback(f"[CPA] {text}")

    try:
        _cpa_log(f"SSO → 换 token (proxy={proxy}) ...")
        token = _s2cpa.sso_to_token(sso, proxy=proxy, log=_cpa_log)
        if not token:
            _cpa_log("换 token 失败，跳过")
            return
        # 兼容授权码流程（token_to_cpa_record 接受 sso 参数）与 device-flow（不接受）
        try:
            import inspect

            _accepts_sso = "sso" in inspect.signature(
                _s2cpa.token_to_cpa_record
            ).parameters
        except (TypeError, ValueError):
            _accepts_sso = False
        if _accepts_sso:
            record = _s2cpa.token_to_cpa_record(token, email=email, sso=sso)
        else:
            record = _s2cpa.token_to_cpa_record(token, email=email)
        try:
            ap = _s2cpa.decode_jwt_payload(record.get("access_token", ""))
            ref = ap.get("referrer")
            if ref == "grok-build":
                _cpa_log("access_token referrer=grok-build OK")
            elif ref:
                _cpa_log(f"提示: access_token referrer={ref!r}")
        except Exception:
            pass
        if auth_dir:
            try:
                path = _s2cpa.write_cpa_auth(_s2cpa.Path(auth_dir), record)
                _cpa_log(f"已写入本地 {path}")
            except Exception as local_exc:
                _cpa_log(f"本地写入失败: {local_exc}")
        if remote_url:
            try:
                name = _s2cpa.upload_cpa_auth_remote(remote_url, management_key, record)
                _cpa_log(f"已上传远程 {remote_url.rstrip('/')}/.../{name}")
            except Exception as remote_exc:
                _cpa_log(f"远程上传失败: {remote_exc}")
    except Exception as exc:
        _cpa_log(f"直出失败: {exc}")


def add_sso_to_sub2api(raw_token, email="", log_callback=None):
    """SSO → 换 token → 验活 →（可选）写导入包 / 远程创建。

    返回 Future[bool]：仅当换 token 成功，且（未开验活或验活通过）时为 True。
    注册主流程应 future.result() 后再计入成功；仅拿到 SSO 不算成功。
    """
    global _sub2api_batch_writer, _sub2api_executor
    from concurrent.futures import Future

    def _done(ok: bool) -> Future:
        fut = Future()
        fut.set_result(bool(ok))
        return fut

    sso = _normalize_sso_token(raw_token)
    if not sso:
        return _done(False)

    export_enabled = bool(config.get("sub2api_auto_add", False))
    out_dir = str(config.get("sub2api_dir", "") or "").strip() if export_enabled else ""
    remote_url = str(config.get("sub2api_url", "") or "").strip() if export_enabled else ""
    admin_token = str(config.get("sub2api_token", "") or "").strip()
    if export_enabled:
        if not out_dir and not remote_url:
            if log_callback:
                log_callback(
                    "[Debug] 已开启 Sub2API 直出但未配置 sub2api_dir 或 sub2api_url，仅验活不计导出"
                )
        if remote_url and not admin_token:
            if log_callback:
                log_callback("[Debug] 已配置 sub2api_url 但未配置 sub2api_token，跳过远程创建")
            remote_url = ""
        if not out_dir and not remote_url:
            # 仍做换 token + 验活，作为成功门槛
            pass

    proxy = str(config.get("proxy", "") or "").strip()
    # 默认验活；关闭后改为「换 token 成功」即算通过
    verify_enabled = bool(config.get("sub2api_verify", True))

    def _s2_log(message):
        if log_callback:
            text = str(message).strip()
            try:
                text.encode("gbk")
            except UnicodeEncodeError:
                text = text.encode("gbk", errors="replace").decode("gbk")
            log_callback(f"[Sub2API] {text}")

    if export_enabled and out_dir and (
        _sub2api_executor is None
        or _sub2api_batch_writer is None
        or _sub2api_batch_writer.output_root.resolve() != _s2cpa.Path(out_dir).resolve()
    ):
        begin_sub2api_batch_session(log_callback=log_callback)
    elif _sub2api_executor is None and (verify_enabled or export_enabled):
        begin_sub2api_batch_session(log_callback=log_callback)

    job = {
        "sso": sso,
        "email": email,
        "proxy": proxy,
        "out_dir": out_dir,
        "remote_url": remote_url,
        "admin_token": admin_token,
        "verify_enabled": verify_enabled,
        "export_enabled": export_enabled and bool(out_dir or remote_url),
    }

    def _worker(payload) -> bool:
        label = payload["email"] or "account"
        try:
            _s2_log(f"{label}: SSO → 换 token ...")
            token = _s2cpa.sso_to_token(
                payload["sso"], proxy=payload["proxy"], log=_s2_log
            )
            if not token:
                _s2_log(f"{label}: 换 token 失败")
                return False
            creds = _s2cpa.token_to_sub2api_credentials(
                token, email=payload["email"]
            )
            if payload["verify_enabled"]:
                _s2_log(f"{label}: 验活中 ...")
                ok, message = _s2cpa.verify_grok_credentials(
                    creds, proxy=payload["proxy"]
                )
                if not ok:
                    _s2_log(f"{label}: 验活失败 ({message})")
                    return False
                _s2_log(f"{label}: 验活通过 ({message})")
            if payload["export_enabled"]:
                with _sub2api_io_lock:
                    if payload["out_dir"]:
                        writer = _sub2api_batch_writer
                        if writer is None:
                            raise RuntimeError("未能创建 Sub2API 批次写入器")
                        result = writer.add_credentials(creds, name=payload["email"])
                        _s2_log(
                            f"{label}: 已写入第 {result['package_index']:03d} 包 "
                            f"({result['position']}/{result['batch_size']}): {result['path']}"
                        )
                    if payload["remote_url"]:
                        created = _s2cpa.upload_sub2api_account(
                            payload["remote_url"],
                            payload["admin_token"],
                            creds,
                            name=payload["email"],
                        )
                        created_id = ""
                        if isinstance(created, dict):
                            data = (
                                created.get("data")
                                if isinstance(created.get("data"), dict)
                                else created
                            )
                            created_id = str((data or {}).get("id") or "")
                        _s2_log(
                            f"{label}: 已创建远程账号"
                            f"{(' id=' + created_id) if created_id else ''}"
                        )
            return True
        except Exception as exc:
            _s2_log(f"{label}: 直出失败: {exc}")
            return False

    executor = _sub2api_executor
    if executor is None:
        return _done(_worker(job))
    future = executor.submit(_worker, job)
    with _sub2api_futures_lock:
        _sub2api_futures.append(future)
    _s2_log(f"{email or 'account'}: 已提交换 token/验活")
    return future


def wait_sub2api_account_result(future, timeout=None) -> bool:
    """等待单账号验活 Future；失败或异常视为 False。"""
    if future is None:
        return False
    try:
        return bool(future.result(timeout=timeout))
    except Exception:
        return False


def create_browser_options():
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    if os.path.exists(EXTENSION_PATH):
        options.add_extension(EXTENSION_PATH)
    return options


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def http_get(url, **kwargs):
    try:
        return requests.get(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        # 代理不可用时自动回退为直连，避免整个流程直接失败
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.get(url, **_build_request_kwargs(**retry_kwargs))
        raise


def http_post(url, **kwargs):
    try:
        return requests.post(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.post(url, **_build_request_kwargs(**retry_kwargs))
        raise


def http_delete(url, **kwargs):
    try:
        return requests.delete(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.delete(url, **_build_request_kwargs(**retry_kwargs))
        raise


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("用户停止注册")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def get_domains(api_key=None):
    return duckmail_provider.get_domains(
        http_get,
        get_duckmail_api_base(),
        api_key=api_key or get_duckmail_api_key(),
    )


def create_account(address, password, api_key=None, expires_in=0):
    return duckmail_provider.create_account(
        http_post,
        get_duckmail_api_base(),
        address,
        password,
        api_key=api_key or get_duckmail_api_key(),
        expires_in=expires_in,
    )


def get_token(address, password):
    return duckmail_provider.get_token(
        http_post,
        get_duckmail_api_base(),
        address,
        password,
    )


def get_messages(token):
    return duckmail_provider.get_messages(
        http_get,
        get_duckmail_api_base(),
        token,
    )


def get_message_detail(token, message_id):
    return duckmail_provider.get_message_detail(
        http_get,
        get_duckmail_api_base(),
        token,
        message_id,
    )



def cloudflare_get_domains(api_base, api_key=None):
    return cloudflare_provider.get_domains(
        http_get,
        api_base,
        domains_path=get_cloudflare_path("cloudflare_path_domains", "/domains"),
        api_key=api_key or get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
    )


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    return cloudflare_provider.create_account(
        http_post,
        api_base,
        address,
        password,
        accounts_path=get_cloudflare_path("cloudflare_path_accounts", "/accounts"),
        api_key=api_key or get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
        expires_in=expires_in,
    )


def cloudflare_get_token(api_base, address, password, api_key=None):
    return cloudflare_provider.get_token(
        http_post,
        api_base,
        address,
        password,
        token_path=get_cloudflare_path("cloudflare_path_token", "/token"),
        api_key=api_key or get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
    )


def cloudflare_get_messages(api_base, token):
    return cloudflare_provider.get_messages(
        http_get,
        api_base,
        token,
        messages_path=get_cloudflare_path("cloudflare_path_messages", "/messages"),
        api_key=get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
    )


def cloudflare_get_message_detail(api_base, token, message_id):
    return cloudflare_provider.get_message_detail(
        http_get,
        api_base,
        token,
        message_id,
        messages_path=get_cloudflare_path("cloudflare_path_messages", "/messages"),
        api_key=get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
    )


YYDS_API_BASE = yyds_provider.API_BASE


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def get_yyds_default_domain():
    return str(config.get("yyds_default_domain", "") or "").strip()


def yyds_get_domains(api_key=None, jwt=None):
    return yyds_provider.get_domains(http_get, api_key=api_key or get_yyds_api_key(), jwt=jwt or get_yyds_jwt())


def yyds_create_account(local_part=None, domain=None, api_key=None, jwt=None):
    return yyds_provider.create_account(
        http_post,
        local_part=local_part or "",
        domain=domain or "",
        api_key=api_key or get_yyds_api_key(),
        jwt=jwt or get_yyds_jwt(),
    )


def yyds_get_token(address, api_key=None, jwt=None):
    return yyds_provider.get_token(http_post, address, api_key=api_key or get_yyds_api_key(), jwt=jwt or get_yyds_jwt())


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    return yyds_provider.get_messages(
        http_get,
        address,
        token=token or "",
        api_key=api_key or get_yyds_api_key(),
        jwt=jwt or get_yyds_jwt(),
    )


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    return yyds_provider.get_message_detail(
        http_get,
        message_id,
        token=token or "",
        api_key=api_key or get_yyds_api_key(),
        jwt=jwt or get_yyds_jwt(),
    )


def yyds_generate_username(length=10):
    return yyds_provider.generate_username(length)


def yyds_pick_domain(api_key=None, jwt=None):
    return yyds_provider.pick_domain(http_get, api_key=api_key or get_yyds_api_key(), jwt=jwt or get_yyds_jwt())


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = get_yyds_default_domain() or yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        local_part=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("获取 YYDS token 失败")
    print(f"[*] 已创建 YYDS 邮箱: {address}")
    return address, temp_token


def yyds_get_oai_code(token, address, timeout=180, poll_interval=3, log_callback=None, jwt=None, cancel_callback=None):
    return yyds_provider.wait_for_code(
        http_get,
        token,
        address,
        timeout=timeout,
        poll_interval=poll_interval,
        jwt=jwt or get_yyds_jwt(),
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )



def generate_username(length=10):
    return _generate_username(length)


def pick_domain(api_key=None):
    return duckmail_provider.pick_domain(get_domains(api_key=api_key))


def get_cloudmail_url():
    return str(os.environ.get("CLOUDMAIL_URL") or config.get("cloudmail_url", "") or "").strip().rstrip("/")


def get_cloudmail_admin_email():
    return str(os.environ.get("CLOUDMAIL_ADMIN_EMAIL") or config.get("cloudmail_admin_email", "") or "").strip()


def get_cloudmail_password():
    return str(os.environ.get("CLOUDMAIL_PASSWORD") or config.get("cloudmail_password", "") or "")


def cloudmail_get_email_and_token():
    raw_domains = str(config.get("defaultDomains", "") or "")
    domains = [item.strip() for item in re.split(r"[,，\s]+", raw_domains) if item.strip()]
    return cloudmail_provider.create_mailbox(
        http_post,
        get_cloudmail_url(),
        get_cloudmail_admin_email(),
        get_cloudmail_password(),
        domains,
        username=generate_username(10),
    )


def cloudmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    del dev_token
    return cloudmail_provider.wait_for_code(
        http_post,
        http_delete,
        get_cloudmail_url(),
        get_cloudmail_admin_email(),
        get_cloudmail_password(),
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=resend_callback,
    )


def get_email_provider():
    return config.get("email_provider", "cloudflare")


def get_email_and_token(api_key=None):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider == "cloudmail":
        return cloudmail_get_email_and_token()
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            try:
                return cloudflare_provider.create_mailbox_fallback(
                    http_get,
                    http_post,
                    api_base,
                    domains_path=get_cloudflare_path("cloudflare_path_domains", "/domains"),
                    accounts_path=get_cloudflare_path("cloudflare_path_accounts", "/accounts"),
                    token_path=get_cloudflare_path("cloudflare_path_token", "/token"),
                    api_key=api_key or get_cloudflare_api_key(),
                    auth_mode=get_cloudflare_auth_mode(),
                    custom_auth=get_cloudflare_custom_auth(),
                )
            except Exception:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
    if provider == "mailnest":
        return mailnest_buy_email(), "_"
    return duckmail_provider.create_mailbox(
        http_get,
        http_post,
        get_duckmail_api_base(),
        api_key=api_key or get_duckmail_api_key(),
        expires_in=0,
    )


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "cloudmail":
        return cloudmail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "mailnest":
        return mailnest_get_code(
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )



def extract_verification_code(text, subject=""):
    return _extract_code(text, subject)


def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    return duckmail_provider.wait_for_code(
        http_get,
        get_duckmail_api_base(),
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        extract_code=extract_verification_code,
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    return cloudflare_provider.wait_for_code(
        http_get,
        get_cloudflare_api_base(),
        dev_token,
        email,
        messages_path=get_cloudflare_path("cloudflare_path_messages", "/messages"),
        api_key=get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
        timeout=timeout,
        poll_interval=poll_interval,
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=resend_callback,
    )


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def response_preview(res, limit=200):
    """安全预览 HTTP 响应体；gRPC/二进制内容不直接当文本打印。"""
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(getattr(res, "headers", {}) or {}).items()}
        content_type = headers.get("content-type", "")
        raw = getattr(res, "content", None)
        if raw is None:
            try:
                raw = (res.text or "").encode("utf-8", errors="replace")
            except Exception:
                raw = b""
        if not isinstance(raw, (bytes, bytearray)):
            raw = str(raw).encode("utf-8", errors="replace")
        raw = bytes(raw)

        # gRPC / protobuf 常见 content-type 或正文以不可打印字节为主
        is_binaryish = (
            "grpc" in content_type
            or "protobuf" in content_type
            or "octet-stream" in content_type
            or (raw[:1] in (b"\x00", b"\x01") and b"grpc-status" in raw)
        )
        if is_binaryish or (raw and sum(1 for b in raw[:64] if b < 9 or (13 < b < 32)) > 8):
            # 尽量抽出可读的 trailer 片段（如 grpc-status:0）
            readable = re.findall(rb"[ -~]{3,}", raw)
            text = " ".join(part.decode("ascii", errors="ignore") for part in readable)
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                text = f"<binary {len(raw)} bytes>"
            return text[:limit]

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]
    except Exception:
        return ""


def is_cloudflare_block_response(res):
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(res.headers).items()}
        text = str(res.text or "").lower()
        server = headers.get("server", "")
        content_type = headers.get("content-type", "")
        return (
            res.status_code in (403, 429, 503)
            and (
                "cloudflare" in server
                or "cloudflare" in text
                or "cf-error" in text
                or "__cf_chl" in text
                or "text/html" in content_type
            )
        )
    except Exception:
        return False


def set_birth_date(session, log_callback=None):
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        body_preview = response_preview(res)
        if log_callback:
            log_callback(
                f"[Debug] set_birth_date status: {res.status_code}, body: {body_preview}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        # 生日一旦写过就不能改；算已完成，不能当失败中断后续 NSFW
        text = str(res.text or "")
        if res.status_code in (400, 409, 429) and (
            "birth-date-change-limit-reached" in text
            or "Birth date is locked" in text
            or "already set" in text.lower()
        ):
            return True, "already_set"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_birth_date 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_birth_date HTTP {res.status_code}: {body_preview}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False, f"set_birth_date 异常: {e}"


def set_tos_accepted(session, log_callback=None):
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_tos_accepted 被 accounts.x.ai 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_tos_accepted HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False, f"set_tos_accepted 异常: {e}"


def encode_grpc_nsfw_settings():
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] update_nsfw status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "update_nsfw_settings 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"update_nsfw_settings HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False, f"update_nsfw_settings 异常: {e}"


def enable_nsfw_for_token(token, cf_clearance="", user_agent="", log_callback=None):
    proxies = get_proxies()
    # cf_clearance 与签发它的浏览器 UA 严格绑定，优先用注册浏览器的真实 UA
    ua = user_agent or get_user_agent()
    try:
        with requests.Session(impersonate="chrome120", proxies=proxies) as session:
            cookie_parts = [f"sso={token}", f"sso-rw={token}"]
            if cf_clearance:
                cookie_parts.append(f"cf_clearance={cf_clearance}")
            session.headers.update(
                {
                    "user-agent": ua,
                    "cookie": "; ".join(cookie_parts),
                }
            )
            ok, message = set_tos_accepted(session, log_callback)
            if not ok:
                return False, message
            ok, message = set_birth_date(session, log_callback)
            if not ok:
                return False, message
            ok, message = update_nsfw_settings(session, log_callback)
            if not ok:
                return False, message
            return True, "成功开启 NSFW"
    except Exception as e:
        return False, f"异常: {str(e)}"


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_tls = threading.local()


def _active_browser():
    return getattr(_tls, "browser", None)


def _active_page():
    return getattr(_tls, "page", None)


def _set_browser_session(browser_obj=None, page_obj=None):
    _tls.browser = browser_obj
    _tls.page = page_obj


# 兼容旧代码中的 browser / page 名称（读写走线程本地）
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


# 注意：start/stop 必须用 _set_browser_session，不要对 browser/page 直接赋值
browser = _SessionProxy("browser")
page = _SessionProxy("page")


def setup_light_theme(root):
    try:
        root.option_add("*Background", UI_BG)
        root.option_add("*Foreground", UI_FG)
        root.option_add("*selectBackground", UI_ACTIVE_BG)
        root.option_add("*selectForeground", UI_FG)
        root.option_add("*insertBackground", UI_FG)
        root.option_add("*Entry.Background", UI_ENTRY_BG)
        root.option_add("*Text.Background", UI_ENTRY_BG)
        root.option_add("*Menu.Background", UI_ENTRY_BG)
        root.option_add("*Menu.Foreground", UI_FG)
        style = ttk.Style(root)
        available = set(style.theme_names())
        if "clam" in available:
            style.theme_use("clam")
        elif "default" in available:
            style.theme_use("default")
        root.configure(bg=UI_BG)
        style.configure(".", background=UI_BG, foreground=UI_FG, fieldbackground=UI_ENTRY_BG)
        style.configure("TFrame", background=UI_BG)
        style.configure("TLabelframe", background=UI_BG, foreground=UI_FG)
        style.configure("TLabelframe.Label", background=UI_BG, foreground=UI_FG)
        style.configure("TLabel", background=UI_BG, foreground=UI_FG)
        style.configure("TCheckbutton", background=UI_BG, foreground=UI_FG)
        style.configure("TButton", background=UI_BUTTON_BG, foreground=UI_FG)
        style.configure("TEntry", fieldbackground=UI_ENTRY_BG, foreground=UI_FG)
        style.configure("TCombobox", fieldbackground=UI_ENTRY_BG, foreground=UI_FG)
        style.configure("TSpinbox", fieldbackground=UI_ENTRY_BG, foreground=UI_FG)
    except Exception:
        pass


def tk_label(parent, text="", **kwargs):
    return tk.Label(parent, text=text, bg=kwargs.pop("bg", UI_BG), fg=kwargs.pop("fg", UI_FG), **kwargs)


def tk_entry(parent, textvariable=None, width=30, **kwargs):
    return tk.Entry(
        parent,
        textvariable=textvariable,
        width=width,
        bg=UI_ENTRY_BG,
        fg=UI_FG,
        insertbackground=UI_FG,
        disabledbackground="#2f2f2f",
        disabledforeground=UI_MUTED_FG,
        highlightthickness=1,
        highlightbackground="#555555",
        relief=tk.SOLID,
        **kwargs,
    )


def tk_button(parent, text="", command=None, state=tk.NORMAL, **kwargs):
    return tk.Button(
        parent,
        text=text,
        command=command,
        state=state,
        bg=UI_BUTTON_BG,
        fg=UI_FG,
        activebackground=UI_ACTIVE_BG,
        activeforeground=UI_FG,
        disabledforeground="#777777",
        relief=tk.RAISED,
        padx=10,
        pady=3,
        **kwargs,
    )


def tk_checkbutton(parent, text="", variable=None, **kwargs):
    return tk.Checkbutton(
        parent,
        text=text,
        variable=variable,
        bg=UI_BG,
        fg=UI_FG,
        activebackground=UI_BG,
        activeforeground=UI_FG,
        selectcolor="#3d7be0",
        **kwargs,
    )


def tk_option_menu(parent, variable, values, width=12):
    menu = tk.OptionMenu(parent, variable, *values)
    menu.configure(
        width=width,
        bg=UI_ENTRY_BG,
        fg=UI_FG,
        activebackground=UI_ACTIVE_BG,
        activeforeground=UI_FG,
        highlightthickness=1,
        highlightbackground="#555555",
        relief=tk.SOLID,
    )
    menu["menu"].configure(bg=UI_ENTRY_BG, fg=UI_FG, activebackground=UI_ACTIVE_BG, activeforeground=UI_FG)
    return menu


def start_browser(log_callback=None):
    last_exc = None
    for attempt in range(1, 5):
        try:
            browser_obj = Chromium(create_browser_options())
            tabs = browser_obj.get_tabs()
            page_obj = tabs[-1] if tabs else browser_obj.new_tab()
            _set_browser_session(browser_obj, page_obj)
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
                cur = _active_browser()
                if cur is not None:
                    cur.quit(del_data=True)
            except Exception:
                pass
            _set_browser_session(None, None)
            time.sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser():
    current = _active_browser()
    _set_browser_session(None, None)
    if current is None:
        return
    try:
        current.quit(del_data=True)
    except BaseException:
        # KeyboardInterrupt 继承 BaseException，清理阶段必须吞掉，避免 Ctrl+C 刷 traceback
        pass


def restart_browser(log_callback=None):
    stop_browser()
    return start_browser(log_callback=log_callback)


def cleanup_runtime_memory(log_callback=None, reason="定期清理"):
    try:
        if log_callback:
            log_callback(f"[*] {reason}: 关闭浏览器并清理内存")
        stop_browser()
        collected = gc.collect()
        if log_callback:
            log_callback(f"[*] Python GC 已回收对象数: {collected}")
    except BaseException:
        # 退出清理中再收到 Ctrl+C 时静默结束，不向外抛
        try:
            stop_browser()
        except BaseException:
            pass


def refresh_active_page():
    if _active_browser() is None:
        restart_browser()
    try:
        browser_obj = _active_browser()
        tabs = browser_obj.get_tabs()
        if tabs:
            page_obj = tabs[-1]
        else:
            page_obj = browser_obj.new_tab()
        _set_browser_session(browser_obj, page_obj)
    except Exception:
        restart_browser()
    return page


def extract_cf_clearance_and_ua(log_callback=None):
    """从注册浏览器提取 grok.com 的 cf_clearance 及其绑定的真实 UA。

    注册流程能拿到 sso 说明浏览器已通过 grok.com 的 Cloudflare 盾，
    此刻 cf_clearance 就在浏览器 cookie 里，配合真实 UA 可用于后续 NSFW 请求。

    返回:
      - (cf_clearance str, user_agent str)：任一取不到则为空字符串
    """
    cf_clearance = ""
    user_agent = ""
    try:
        active = refresh_active_page()
        if active is None:
            return "", ""
        cookies = active.cookies(all_domains=True, all_info=True) or []
        for item in cookies:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                value = str(item.get("value", "")).strip()
            else:
                name = str(getattr(item, "name", "")).strip()
                value = str(getattr(item, "value", "")).strip()
            if name == "cf_clearance" and value:
                cf_clearance = value
                break
        try:
            ua = active.run_js("return navigator.userAgent;")
            if ua:
                user_agent = str(ua).strip()
        except Exception:
            pass
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 提取 cf_clearance 失败: {exc}")
    return cf_clearance, user_agent


def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
const target = candidates[0]?.node || null;
if (!target) {
    return false;
}
target.click();
return candidates[0].text || true;
        """)

        if clicked:
            if log_callback:
                detail = f": {clicked}" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已点击「使用邮箱注册」按钮{detail}")
            sleep_with_cancel(2, cancel_callback)
            return True

        if log_callback:
            current_url = page.url if page else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        sleep_with_cancel(1, cancel_callback)

    if log_callback:
        page_html = page.html[:500] if page else "no page"
        log_callback(f"[Debug] 页面内容片段: {page_html}")

    raise Exception("未找到「使用邮箱注册」按钮")


def open_signup_page(log_callback=None, cancel_callback=None):
    raise_if_cancelled(cancel_callback)
    if _active_browser() is None:
        start_browser(log_callback=log_callback)
        if log_callback:
            log_callback("[*] 浏览器已启动")

    def _navigate_signup():
        # 优先复用已有标签，避免反复 new_tab 堆积空窗口
        browser_obj = _active_browser()
        if browser_obj is None:
            start_browser(log_callback=log_callback)
            browser_obj = _active_browser()
        try:
            tabs = browser_obj.get_tabs() if browser_obj is not None else []
            page_obj = tabs[-1] if tabs else browser_obj.new_tab()
        except Exception:
            page_obj = browser_obj.new_tab()
        _set_browser_session(browser_obj, page_obj)
        page_obj.get(SIGNUP_URL)
        page_obj.wait.doc_loaded()
        # 确认真的进了注册域；about:blank / 错页直接失败
        current = str(getattr(page_obj, "url", "") or "")
        if "accounts.x.ai" not in current and "x.ai" not in current:
            raise Exception(f"打开注册页失败，当前URL: {current or 'empty'}")

    try:
        _navigate_signup()
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        try:
            restart_browser(log_callback=log_callback)
            _navigate_signup()
        except Exception as e2:
            # 导航彻底失败：关掉残留实例，避免空浏览器挂着
            try:
                stop_browser()
            except Exception:
                pass
            raise Exception(f"打开注册页失败: {e2}") from e2

    sleep_with_cancel(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {_active_page().url if _active_page() else ''}")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )


def has_profile_form(log_callback=None):
    refresh_active_page()
    try:
        return bool(
            page.run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


def detect_email_domain_rejection(email=""):
    """检测 xAI 是否拒绝当前邮箱域名。

    返回拒绝文案字符串；未检测到则返回空字符串。
    """
    if not page:
        return ""
    try:
        result = page.run_js(
            r"""
function collectText() {
    const chunks = [];
    const selectors = [
        '[role="alert"]',
        '[data-testid*="error" i]',
        '[class*="error" i]',
        '[class*="Error"]',
        '[class*="danger" i]',
        '[class*="invalid" i]',
        'p', 'span', 'div', 'li', 'label',
    ];
    for (const sel of selectors) {
        for (const node of Array.from(document.querySelectorAll(sel)).slice(0, 80)) {
            const style = window.getComputedStyle(node);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
            if (text && text.length >= 8 && text.length <= 400) chunks.push(text);
        }
    }
    const body = (document.body && (document.body.innerText || document.body.textContent) || '')
        .replace(/\s+/g, ' ').trim();
    if (body) chunks.push(body.slice(0, 1200));
    return Array.from(new Set(chunks));
}
const texts = collectText();
const patterns = [
    /邮箱域名[^。\n]{0,80}被拒绝/,
    /域名[^。\n]{0,40}已被拒绝/,
    /已被拒绝[^。\n]{0,40}邮箱/,
    /email domain[^.\n]{0,80}rejected/i,
    /domain[^.\n]{0,40}(has been |is )?rejected/i,
    /please use (a )?different email/i,
    /use another email address/i,
    /请使用其他邮箱/,
    /support@x\.ai/,
];
for (const text of texts) {
    for (const re of patterns) {
        if (re.test(text)) {
            const m = text.match(/.{0,40}(拒绝|rejected|different email|其他邮箱).{0,80}/i);
            return (m && m[0]) || text.slice(0, 180);
        }
    }
}
return '';
            """
        )
        if isinstance(result, str) and result.strip():
            return result.strip()
    except Exception:
        pass
    return ""


def raise_if_email_domain_rejected(email=""):
    message = detect_email_domain_rejection(email)
    if message:
        raise EmailDomainRejected(email=email, message=message)


def _email_page_advanced_once(email):
    """检测邮箱提交后页面是否真正前进（离开邮箱输入阶段）。

    点击注册按钮只代表触发了点击，不代表表单真的提交成功。
    若 Cloudflare 挑战未过或页面卡住，按钮点击无实际效果，
    邮箱输入框会一直停留，导致后续空等验证码。

    判定“已前进”的依据：
      - 出现验证码输入框（OTP / code 输入），或
      - 原本可见可用的邮箱输入框已消失/不可用

    返回:
      - True：页面已前进，提交生效
      - False：仍停留在邮箱输入页
    """
    # 域名被拒时仍停在邮箱页，优先抛出明确错误
    raise_if_email_domain_rejected(email)
    try:
        return bool(
            page.run_js(
                """
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.getAttribute('aria-label'),
        node.getAttribute('placeholder'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
        node.getAttribute('data-testid'),
    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
}
// 1. 出现验证码输入框 => 已前进
const codeInput = Array.from(document.querySelectorAll('input')).find((node) => {
    if (!isVisible(node)) return false;
    const type = (node.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file'].includes(type)) return false;
    const meta = textOf(node);
    const inMode = (node.getAttribute('inputmode') || '').toLowerCase();
    return (
        meta.includes('code') || meta.includes('otp') || meta.includes('verif') ||
        meta.includes('验证') || meta.includes('one-time') || inMode === 'numeric' ||
        node.getAttribute('autocomplete') === 'one-time-code'
    );
});
if (codeInput) return true;
// 2. 邮箱输入框已消失/不可用 => 已前进
const emailInput = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'))
    .find((node) => isVisible(node) && !node.disabled && !node.readOnly);
if (!emailInput) return true;
return false;
                """
            )
        )
    except EmailDomainRejected:
        raise
    except Exception:
        return False


def _wait_email_page_advanced(email, wait=4.0, cancel_callback=None):
    """点击提交后，在有限窗口内轮询确认页面确实前进。

    给页面/网络一点反应时间：若窗口内检测到已前进则返回 True，
    否则返回 False，由调用方继续重试点击或最终超时换邮箱。
    """
    deadline = time.time() + wait
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        raise_if_email_domain_rejected(email)
        if _email_page_advanced_once(email):
            return True
        sleep_with_cancel(0.4, cancel_callback)
    raise_if_email_domain_rejected(email)
    return False


def fill_email_and_submit(timeout=45, log_callback=None, cancel_callback=None):
    raise_if_cancelled(cancel_callback)
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    deadline = time.time() + timeout
    last_diag_time = 0
    last_reclick_time = 0
    last_snapshot = None
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            r"""
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function describeInput(node) {
    return [
        `type=${node.getAttribute('type') || ''}`,
        `name=${node.getAttribute('name') || ''}`,
        `id=${node.getAttribute('id') || ''}`,
        `placeholder=${node.getAttribute('placeholder') || ''}`,
        `aria=${node.getAttribute('aria-label') || ''}`,
        `testid=${node.getAttribute('data-testid') || ''}`,
    ].join(' ').replace(/\s+/g, ' ').trim().slice(0, 160);
}
function describeAction(node) {
    return textOf(node).slice(0, 120);
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const visibleInputs = Array.from(document.querySelectorAll('input, textarea'))
    .filter((node) => isVisible(node) && !node.disabled && !node.readOnly)
    .map(describeInput)
    .slice(0, 8);
const visibleActions = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map(describeAction)
    .filter(Boolean)
    .slice(0, 10);
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) {
    return {
        state: 'not-ready',
        url: location.href,
        title: document.title,
        inputs: visibleInputs,
        buttons: visibleActions,
    };
}
input.focus(); input.click();
const valueProto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const valueSetter = Object.getOwnPropertyDescriptor(valueProto, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
const inputType = (input.getAttribute('type') || '').toLowerCase();
const isValid = inputType !== 'email' || input.checkValidity();
if ((input.value || '').trim() !== email || !isValid) {
    return {
        state: 'fill-failed',
        value: input.value || '',
        valid: isValid,
        input: describeInput(input),
        url: location.href,
    };
}
input.blur();
return {
    state: 'filled',
    input: describeInput(input),
    url: location.href,
};
            """,
            email,
        )
        state = filled.get("state") if isinstance(filled, dict) else filled
        if isinstance(filled, dict):
            last_snapshot = filled
        if state == "not-ready":
            now = time.time()
            if now - last_reclick_time >= 3:
                reclicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
if (!candidates.length) return false;
candidates[0].node.click();
return candidates[0].text || true;
                """)
                last_reclick_time = now
                if reclicked and log_callback:
                    detail = f": {reclicked}" if isinstance(reclicked, str) else ""
                    log_callback(f"[Debug] 邮箱输入框未出现，已再次触发邮箱注册入口{detail}")
            if log_callback and now - last_diag_time >= 5:
                last_diag_time = now
                inputs = " | ".join((filled or {}).get("inputs", [])[:6]) if isinstance(filled, dict) else ""
                buttons = " | ".join((filled or {}).get("buttons", [])[:8]) if isinstance(filled, dict) else ""
                url = (filled or {}).get("url", page.url if page else "") if isinstance(filled, dict) else (page.url if page else "")
                log_callback(f"[Debug] 等待邮箱输入框: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if state != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        sleep_with_cancel(0.8, cancel_callback)
        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !(input.value || '').trim()) return false;
const inputType = (input.getAttribute('type') || '').toLowerCase();
if (inputType === 'email' && !input.checkValidity()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find((node) => {
    const text = textOf(node).replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        text.includes('继续') ||
        text.includes('下一步') ||
        text.includes('确认') ||
        lower.includes('signup') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next') ||
        lower.includes('createaccount') ||
        lower.includes('submit')
    );
});
if (submitButton) {
    submitButton.click();
    return textOf(submitButton) || true;
}
const form = input.closest('form');
if (form) {
    if (form.requestSubmit) form.requestSubmit();
    else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    return 'form-submit';
}
input.focus();
input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
return 'enter';
            """
        )
        if clicked:
            # 点击按钮 != 表单真正提交成功：CF 挑战未过或页面卡住时点击无效果，
            # 邮件不会发出。必须确认页面已离开邮箱输入阶段（邮箱框消失或出现验证码框），
            # 否则继续循环重试点击，最终超时抛异常触发换邮箱重试。
            if _wait_email_page_advanced(email, cancel_callback=cancel_callback):
                if log_callback:
                    detail = f" ({clicked})" if isinstance(clicked, str) else ""
                    log_callback(f"[*] 已填写邮箱并提交: {email}{detail}")
                return email, dev_token
            if log_callback and time.time() - last_diag_time >= 5:
                last_diag_time = time.time()
                log_callback(f"[Debug] 已点击注册但页面未前进，重试提交: {email}")
            raise_if_email_domain_rejected(email)
        sleep_with_cancel(0.5, cancel_callback)
    raise_if_email_domain_rejected(email)
    if last_snapshot:
        inputs = " | ".join(last_snapshot.get("inputs", [])[:6])
        buttons = " | ".join(last_snapshot.get("buttons", [])[:8])
        url = last_snapshot.get("url", page.url if page else "")
        raise Exception(
            f"未找到邮箱输入框或注册按钮，最后页面: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}"
        )
    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(email, dev_token, timeout=180, log_callback=None, cancel_callback=None):
    def _resend_code():
        page.run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

    code = get_oai_code(
        dev_token,
        email,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue

        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next')
    );
});

if (!btn) return 'no-button';
btn.focus();
btn.click();
return 'clicked';
            """
        )

        if clicked == "clicked" or clicked == "no-button":
            if log_callback:
                log_callback(f"[*] 已填写验证码并提交: {code}")
            sleep_with_cancel(1.5, cancel_callback)
            return code

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("验证码已获取，但自动填写/提交失败")


def getTurnstileToken(log_callback=None, cancel_callback=None):
    if _active_page() is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    try:
        page.run_js(
            "try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {}"
        )
    except Exception:
        pass

    for _ in range(0, 20):
        raise_if_cancelled(cancel_callback)
        try:
            token = page.run_js(
                """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    return String(turnstile.getResponse() || '').trim();
  }
  return '';
} catch(e) { return ''; }
                """
            )
            token = str(token or "").strip()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            challenge_input = page.ele("@name=cf-turnstile-response")
            if challenge_input:
                wrapper = challenge_input.parent()
                iframe = None
                try:
                    iframe = wrapper.shadow_root.ele("tag:iframe")
                except Exception:
                    iframe = None
                if iframe:
                    try:
                        iframe.run_js(
                            """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                            """
                        )
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input")
                        if btn:
                            btn.click()
                    except Exception:
                        pass
            else:
                # 兜底：尝试触发页面上可见的 Turnstile 容器
                page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter((n) => {
  const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
  return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
                    """
                )
        except Exception:
            pass
        sleep_with_cancel(1, cancel_callback)

    raise Exception("Turnstile 获取 token 失败")


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=120, log_callback=None, cancel_callback=None):
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not form_filled_once:
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

if (submitBtn) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
            """,
                given_name,
                family_name,
                password,
            )

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                if log_callback:
                    token_len = filled.split(":", 1)[1] if ":" in filled else "0"
                    log_callback(f"[*] 资料已填写，等待 Cloudflare 人机验证通过... 当前token长度={token_len}")
                if token_len == "0":
                    pause_seconds = random.uniform(1, 3)
                    if log_callback:
                        log_callback(f"[*] Cloudflare token 为空，暂停 {pause_seconds:.1f}s 后继续检测")
                    sleep_with_cancel(pause_seconds, cancel_callback)
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                # 卡住后自动二次复用 Turnstile 组件
                if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                    if log_callback:
                        log_callback("[*] Cloudflare 验证卡住，开始二次复用 Turnstile...")
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            synced = page.run_js(
                                """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                """,
                                token,
                            )
                            if log_callback:
                                log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                    last_cf_retry_at = now
                sleep_with_cancel(0.8, cancel_callback)
                continue

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                sleep_with_cancel(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                sleep_with_cancel(0.5, cancel_callback)
                continue

        submit_state = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'no-submit-button:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'submitted';
            """
        )

        if isinstance(submit_state, str) and submit_state.startswith("wait-cloudflare"):
            if log_callback:
                token_len = submit_state.split(":", 1)[1] if ":" in submit_state else "0"
                log_callback(f"[*] 等待 Cloudflare 人机验证通过后再提交... 当前token长度={token_len}")
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                if log_callback:
                    log_callback("[*] 提交前仍卡住，自动再次复用 Turnstile...")
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        synced = page.run_js(
                            """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                            """,
                            token,
                        )
                        if log_callback:
                            log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                last_cf_retry_at = now
            sleep_with_cancel(0.8, cancel_callback)
            continue

        if submit_state == "submitted":
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        wait_cf_since = None
        if isinstance(submit_state, str) and submit_state.startswith("no-submit-button") and log_callback:
            visible_buttons = submit_state.split(":", 1)[1] if ":" in submit_state else ""
            suffix = f" 可见按钮: {visible_buttons}" if visible_buttons else ""
            log_callback(f"[Debug] 未找到提交按钮，继续等待页面稳定...{suffix}")

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("最终注册页资料填写失败")


def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0
    final_no_submit_state = ""
    final_no_submit_since = None
    final_no_submit_timeout = 25

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            refresh_active_page()
            if _active_page() is None:
                sleep_with_cancel(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    const lower = t.toLowerCase();
    return t.includes('完成注册') || lower.includes('completeyoursignup') || lower.includes('completesignup');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'final-page-no-submit:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and (retried == "final-page-clicked-submit" or (isinstance(retried, str) and retried.startswith("final-page-no-submit"))):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if isinstance(retried, str) and retried.startswith("final-page-no-submit"):
                    if retried != final_no_submit_state:
                        final_no_submit_state = retried
                        final_no_submit_since = now
                    elif final_no_submit_since and now - final_no_submit_since >= final_no_submit_timeout:
                        raise AccountRetryNeeded(
                            f"最终注册页状态 {final_no_submit_timeout}s 未变化且未找到提交按钮，重试当前账号: {retried}"
                        )
                else:
                    final_no_submit_state = ""
                    final_no_submit_since = None
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 10:
                        if log_callback:
                            log_callback("[*] 最终页 Cloudflare 卡住，自动二次复用 Turnstile...")
                        try:
                            token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                            if token:
                                synced = page.run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(f"[*] 最终页 Turnstile 二次复用完成，回填长度={synced}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 二次复用失败: {cf_exc}")
                        last_cf_retry_at = now

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[*] 已获取到 sso cookie")
                    return value
        except PageDisconnectedError:
            refresh_active_page()
        except AccountRetryNeeded:
            raise
        except RegistrationCancelled:
            raise
        except Exception:
            pass

        sleep_with_cancel(1, cancel_callback)

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


class GrokRegisterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Grok 注册机")
        self.root.geometry("1120x900")
        self.root.minsize(960, 700)
        self.is_running = False
        self.batch_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        self.stop_requested = False
        self.ui_queue = queue.Queue()
        self.accounts_output_file = ""
        self.setup_ui()

    def setup_ui(self):
        load_config()
        main_frame = tk.Frame(self.root, bg=UI_BG, padx=10, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.grid_columnconfigure(0, weight=1)
        main_frame.grid_rowconfigure(3, weight=1)

        config_frame = tk.LabelFrame(
            main_frame,
            text="配置",
            bg=UI_PANEL_BG,
            fg=UI_FG,
            padx=10,
            pady=10,
            relief=tk.GROOVE,
            borderwidth=1,
        )
        config_frame.grid(row=0, column=0, sticky=tk.EW, pady=(0, 8))
        config_frame.grid_columnconfigure(1, weight=1, minsize=260)
        config_frame.grid_columnconfigure(3, weight=1, minsize=260)

        def add_label(row, column, text):
            tk_label(config_frame, text=text, bg=UI_PANEL_BG).grid(
                row=row,
                column=column,
                sticky=tk.W,
                padx=(0, 6),
                pady=3,
            )

        def add_field(widget, row, column, columnspan=1, sticky=tk.EW):
            widget.grid(
                row=row,
                column=column,
                columnspan=columnspan,
                sticky=sticky,
                padx=(0, 14),
                pady=3,
            )

        # 公共配置
        add_label(0, 0, "邮箱服务商:")
        self.email_provider_var = tk.StringVar(value=config.get("email_provider", "cloudflare"))
        self.email_provider_combo = tk_option_menu(
            config_frame,
            self.email_provider_var,
            ["duckmail", "yyds", "cloudflare", "mailnest", "cloudmail"],
            width=12,
        )
        add_field(self.email_provider_combo, 0, 1, sticky=tk.W)

        add_label(0, 2, "注册数量:")
        self.count_var = tk.StringVar(value=str(config.get("register_count", 1)))
        self.count_spinbox = tk.Spinbox(
            config_frame,
            from_=1,
            to=2500,
            width=8,
            textvariable=self.count_var,
            bg=UI_ENTRY_BG,
            fg=UI_FG,
            insertbackground=UI_FG,
            buttonbackground=UI_BUTTON_BG,
            disabledbackground="#2f2f2f",
            disabledforeground=UI_MUTED_FG,
            relief=tk.SOLID,
        )
        add_field(self.count_spinbox, 0, 3, sticky=tk.W)

        add_label(1, 0, "注册选项:")
        self.nsfw_var = tk.BooleanVar(value=config.get("enable_nsfw", True))
        self.nsfw_check = tk_checkbutton(config_frame, text="注册后开启 NSFW", variable=self.nsfw_var)
        add_field(self.nsfw_check, 1, 1, sticky=tk.W)

        add_label(1, 2, "代理（可选）:")
        self.proxy_var = tk.StringVar(value=config.get("proxy", ""))
        self.proxy_entry = tk_entry(config_frame, textvariable=self.proxy_var, width=34)
        add_field(self.proxy_entry, 1, 3)

        # 服务商专属配置（按选择显示）
        self.provider_frame = tk.LabelFrame(
            config_frame,
            text="邮箱服务商配置",
            bg=UI_PANEL_BG,
            fg=UI_FG,
            padx=8,
            pady=6,
            relief=tk.GROOVE,
            borderwidth=1,
        )
        self.provider_frame.grid(row=2, column=0, columnspan=4, sticky=tk.EW, pady=(6, 4))
        self.provider_frame.grid_columnconfigure(1, weight=1, minsize=240)
        self.provider_frame.grid_columnconfigure(3, weight=1, minsize=240)

        def p_label(row, column, text):
            w = tk_label(self.provider_frame, text=text, bg=UI_PANEL_BG)
            w.grid(row=row, column=column, sticky=tk.W, padx=(0, 6), pady=3)
            return w

        def p_field(widget, row, column, columnspan=1, sticky=tk.EW):
            widget.grid(
                row=row,
                column=column,
                columnspan=columnspan,
                sticky=sticky,
                padx=(0, 14),
                pady=3,
            )
            return widget

        # DuckMail / Mail.tm
        self.api_key_var = tk.StringVar(value=config.get("duckmail_api_key", ""))
        self.duckmail_api_base_var = tk.StringVar(
            value=str(config.get("duckmail_api_base", "") or DUCKMAIL_API_BASE_DEFAULT)
        )
        self._duckmail_widgets = [
            p_label(0, 0, "API Base:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.duckmail_api_base_var, width=52), 0, 1, columnspan=3),
            p_label(1, 0, "API Key (可选):"),
            p_field(tk_entry(self.provider_frame, textvariable=self.api_key_var, width=34), 1, 1),
            p_label(1, 2, "说明:"),
            p_field(
                tk_label(
                    self.provider_frame,
                    text="Mail.tm 填 https://api.mail.tm；DuckMail 默认 api.duckmail.sbs",
                    bg=UI_PANEL_BG,
                ),
                1,
                3,
                sticky=tk.W,
            ),
        ]

        # Cloudflare
        self.cloudflare_auth_mode_var = tk.StringVar(value=config.get("cloudflare_auth_mode", "none"))
        self.cloudflare_api_base_var = tk.StringVar(value=config.get("cloudflare_api_base", ""))
        self.cloudflare_api_key_var = tk.StringVar(value=config.get("cloudflare_api_key", ""))
        self.cloudflare_paths_var = tk.StringVar(
            value=",".join(
                [
                    config.get("cloudflare_path_domains", "/api/domains"),
                    config.get("cloudflare_path_accounts", "/api/new_address"),
                    config.get("cloudflare_path_token", "/api/token"),
                    config.get("cloudflare_path_messages", "/api/mails"),
                ]
            )
        )
        self.default_domains_var = tk.StringVar(value=str(config.get("defaultDomains", "")))
        self.cloudflare_custom_auth_var = tk.StringVar(value=str(config.get("cloudflare_custom_auth", "")))
        self._cloudflare_widgets = [
            p_label(0, 0, "API Base:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.cloudflare_api_base_var, width=52), 0, 1, columnspan=3),
            p_label(1, 0, "鉴权模式:"),
            p_field(
                tk_option_menu(
                    self.provider_frame,
                    self.cloudflare_auth_mode_var,
                    ["query-key", "bearer", "x-api-key", "x-admin-auth", "none"],
                    width=12,
                ),
                1,
                1,
                sticky=tk.W,
            ),
            p_label(1, 2, "API Key:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.cloudflare_api_key_var, width=34), 1, 3),
            p_label(2, 0, "收信域名:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.default_domains_var, width=34), 2, 1),
            p_label(2, 2, "全局密码:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.cloudflare_custom_auth_var, width=34), 2, 3),
            p_label(3, 0, "CF 路径:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.cloudflare_paths_var, width=52), 3, 1, columnspan=3),
        ]

        # YYDS
        self.yyds_api_key_var = tk.StringVar(value=str(config.get("yyds_api_key", "")))
        self.yyds_jwt_var = tk.StringVar(value=str(config.get("yyds_jwt", "")))
        self.yyds_default_domain_var = tk.StringVar(value=str(config.get("yyds_default_domain", "")))
        self._yyds_widgets = [
            p_label(0, 0, "API Key:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.yyds_api_key_var, width=34), 0, 1),
            p_label(0, 2, "JWT:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.yyds_jwt_var, width=34), 0, 3),
            p_label(1, 0, "固定收信域名:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.yyds_default_domain_var, width=34), 1, 1),
            p_label(1, 2, "说明:"),
            p_field(
                tk_label(self.provider_frame, text="域名留空则自动选已验证域", bg=UI_PANEL_BG),
                1,
                3,
                sticky=tk.W,
            ),
        ]

        # MailNest
        self.mailnest_api_key_var = tk.StringVar(value=str(config.get("mailnest_api_key", "")))
        self.mailnest_project_code_var = tk.StringVar(
            value=str(config.get("mailnest_project_code", MAILNEST_DEFAULT_PROJECT_CODE) or MAILNEST_DEFAULT_PROJECT_CODE)
        )
        self._mailnest_widgets = [
            p_label(0, 0, "API Key:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.mailnest_api_key_var, width=34), 0, 1),
            p_label(0, 2, "项目代码:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.mailnest_project_code_var, width=34), 0, 3),
        ]

        # CloudMail
        self.cloudmail_url_var = tk.StringVar(value=str(config.get("cloudmail_url", "")))
        self.cloudmail_admin_email_var = tk.StringVar(value=str(config.get("cloudmail_admin_email", "")))
        self.cloudmail_password_var = tk.StringVar(value=str(config.get("cloudmail_password", "")))
        # CloudMail 也用 defaultDomains；与 CF 共用变量即可
        self._cloudmail_widgets = [
            p_label(0, 0, "站点 URL:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.cloudmail_url_var, width=52), 0, 1, columnspan=3),
            p_label(1, 0, "管理员邮箱:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.cloudmail_admin_email_var, width=34), 1, 1),
            p_label(1, 2, "管理员密码:"),
            p_field(
                tk_entry(self.provider_frame, textvariable=self.cloudmail_password_var, width=34, show="*"),
                1,
                3,
            ),
            p_label(2, 0, "收信域名:"),
            p_field(tk_entry(self.provider_frame, textvariable=self.default_domains_var, width=34), 2, 1),
            p_label(2, 2, "说明:"),
            p_field(
                tk_label(self.provider_frame, text="多个域名用逗号分隔", bg=UI_PANEL_BG),
                2,
                3,
                sticky=tk.W,
            ),
        ]

        self._provider_widget_groups = {
            "duckmail": self._duckmail_widgets,
            "cloudflare": self._cloudflare_widgets,
            "yyds": self._yyds_widgets,
            "mailnest": self._mailnest_widgets,
            "cloudmail": self._cloudmail_widgets,
        }

        add_label(3, 0, "并发数:")
        self.workers_var = tk.StringVar(value=str(config.get("register_workers", 1)))
        self.workers_spinbox = tk.Spinbox(
            config_frame,
            from_=1,
            to=8,
            width=8,
            textvariable=self.workers_var,
            bg=UI_ENTRY_BG,
            fg=UI_FG,
            insertbackground=UI_FG,
            buttonbackground=UI_BUTTON_BG,
            disabledbackground="#2f2f2f",
            disabledforeground=UI_MUTED_FG,
            relief=tk.SOLID,
        )
        add_field(self.workers_spinbox, 3, 1, sticky=tk.W)

        # SSO → CPA auth 可选
        self.cpa_frame = tk.LabelFrame(
            config_frame,
            text="SSO → CPA auth（可选）",
            bg=UI_PANEL_BG,
            fg=UI_FG,
            padx=8,
            pady=6,
            relief=tk.GROOVE,
            borderwidth=1,
        )
        self.cpa_frame.grid(row=4, column=0, columnspan=4, sticky=tk.EW, pady=(6, 2))
        self.cpa_frame.grid_columnconfigure(1, weight=1, minsize=240)
        self.cpa_frame.grid_columnconfigure(3, weight=1, minsize=240)

        self.cpa_auto_add_var = tk.BooleanVar(value=bool(config.get("cpa_auto_add", False)))
        tk_checkbutton(
            self.cpa_frame,
            text="注册成功后将 SSO 转为 CPA auth 并入库",
            variable=self.cpa_auto_add_var,
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=3)

        self._cpa_detail_widgets = []
        def c_label(row, col, text):
            w = tk_label(self.cpa_frame, text=text, bg=UI_PANEL_BG)
            w.grid(row=row, column=col, sticky=tk.W, padx=(0, 6), pady=3)
            self._cpa_detail_widgets.append(w)
            return w

        def c_field(widget, row, col, columnspan=1, sticky=tk.EW):
            widget.grid(row=row, column=col, columnspan=columnspan, sticky=sticky, padx=(0, 14), pady=3)
            self._cpa_detail_widgets.append(widget)
            return widget

        self.cpa_auth_dir_var = tk.StringVar(value=str(config.get("cpa_auth_dir", "")))
        self.cpa_remote_url_var = tk.StringVar(value=str(config.get("cpa_remote_url", "")))
        self.cpa_management_key_var = tk.StringVar(value=str(config.get("cpa_management_key", "")))
        c_label(1, 0, "auth 目录:")
        c_field(tk_entry(self.cpa_frame, textvariable=self.cpa_auth_dir_var, width=52), 1, 1, columnspan=3)
        c_label(2, 0, "远程地址:")
        c_field(tk_entry(self.cpa_frame, textvariable=self.cpa_remote_url_var, width=34), 2, 1)
        c_label(2, 2, "管理密钥:")
        c_field(tk_entry(self.cpa_frame, textvariable=self.cpa_management_key_var, width=28), 2, 3)

        # SSO → Sub2API 可选（导入包 / 远程创建）
        self.sub2api_frame = tk.LabelFrame(
            config_frame,
            text="SSO → Sub2API（可选）",
            bg=UI_PANEL_BG,
            fg=UI_FG,
            padx=8,
            pady=6,
            relief=tk.GROOVE,
            borderwidth=1,
        )
        self.sub2api_frame.grid(row=5, column=0, columnspan=4, sticky=tk.EW, pady=(6, 2))
        self.sub2api_frame.grid_columnconfigure(1, weight=1, minsize=240)
        self.sub2api_frame.grid_columnconfigure(3, weight=1, minsize=240)

        self.sub2api_auto_add_var = tk.BooleanVar(
            value=bool(config.get("sub2api_auto_add", False))
        )
        tk_checkbutton(
            self.sub2api_frame,
            text="注册成功后将 SSO 转为 Sub2API 账号（导入包 / 远程创建）",
            variable=self.sub2api_auto_add_var,
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=3)

        self._sub2api_detail_widgets = []

        def s_label(row, col, text):
            w = tk_label(self.sub2api_frame, text=text, bg=UI_PANEL_BG)
            w.grid(row=row, column=col, sticky=tk.W, padx=(0, 6), pady=3)
            self._sub2api_detail_widgets.append(w)
            return w

        def s_field(widget, row, col, columnspan=1, sticky=tk.EW):
            widget.grid(row=row, column=col, columnspan=columnspan, sticky=sticky, padx=(0, 14), pady=3)
            self._sub2api_detail_widgets.append(widget)
            return widget

        self.sub2api_dir_var = tk.StringVar(value=str(config.get("sub2api_dir", "")))
        self.sub2api_url_var = tk.StringVar(value=str(config.get("sub2api_url", "")))
        self.sub2api_token_var = tk.StringVar(value=str(config.get("sub2api_token", "")))
        self.sub2api_batch_size_var = tk.StringVar(
            value=str(config.get("sub2api_batch_size", 20))
        )
        self.sub2api_verify_var = tk.BooleanVar(
            value=bool(config.get("sub2api_verify", True))
        )
        s_label(1, 0, "导入包目录:")
        s_field(tk_entry(self.sub2api_frame, textvariable=self.sub2api_dir_var, width=52), 1, 1, columnspan=3)
        s_label(2, 0, "远程地址:")
        s_field(tk_entry(self.sub2api_frame, textvariable=self.sub2api_url_var, width=34), 2, 1)
        s_label(2, 2, "管理 Token:")
        s_field(tk_entry(self.sub2api_frame, textvariable=self.sub2api_token_var, width=28), 2, 3)
        s_label(3, 0, "每包数量:")
        s_field(tk_entry(self.sub2api_frame, textvariable=self.sub2api_batch_size_var, width=12), 3, 1, sticky=tk.W)
        self._sub2api_verify_check = tk_checkbutton(
            self.sub2api_frame,
            text="并行验活",
            variable=self.sub2api_verify_var,
        )
        s_field(self._sub2api_verify_check, 3, 2, columnspan=2, sticky=tk.W)

        self.email_provider_var.trace_add("write", lambda *_: self._refresh_provider_fields())
        self.cpa_auto_add_var.trace_add("write", lambda *_: self._refresh_cpa_fields())
        self.sub2api_auto_add_var.trace_add("write", lambda *_: self._refresh_sub2api_fields())
        self._refresh_provider_fields()
        self._refresh_cpa_fields()
        self._refresh_sub2api_fields()

        btn_frame = tk.Frame(main_frame, bg=UI_BG)
        btn_frame.grid(row=1, column=0, sticky=tk.EW, pady=(0, 6))
        self.start_btn = tk_button(btn_frame, text="开始注册", command=self.start_registration)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = tk_button(btn_frame, text="停止", command=self.stop_registration, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.clear_btn = tk_button(btn_frame, text="清空日志", command=self.clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=5)

        status_frame = tk.Frame(main_frame, bg=UI_BG)
        status_frame.grid(row=2, column=0, sticky=tk.EW, pady=(0, 6))
        self.status_var = tk.StringVar(value="就绪")
        tk_label(status_frame, text="状态: ").pack(side=tk.LEFT)
        self.status_label = tk.Label(status_frame, textvariable=self.status_var, bg=UI_BG, fg="green")
        self.status_label.pack(side=tk.LEFT)
        self.stats_var = tk.StringVar(value="成功: 0 | 失败: 0")
        tk.Label(status_frame, textvariable=self.stats_var, bg=UI_BG, fg=UI_FG).pack(side=tk.RIGHT)
        log_frame = tk.LabelFrame(
            main_frame,
            text="日志",
            bg=UI_PANEL_BG,
            fg=UI_FG,
            padx=5,
            pady=5,
            relief=tk.GROOVE,
            borderwidth=1,
        )
        log_frame.grid(row=3, column=0, sticky=tk.NSEW)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=18,
            width=60,
            bg="#111111",
            fg="#f5f5f5",
            insertbackground="#f5f5f5",
            selectbackground="#345a8a",
            selectforeground="#ffffff",
            relief=tk.SOLID,
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#555555",
        )
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        self.log("[*] GUI 已就绪，配置已加载")
        self.log(f"[*] 当前邮箱服务商: {self.email_provider_var.get()} | 注册数量: {self.count_var.get()}")

    def _refresh_provider_fields(self):
        """按当前邮箱服务商只显示相关配置项。"""
        provider = (self.email_provider_var.get() or "cloudflare").strip().lower()
        titles = {
            "duckmail": "DuckMail / Mail.tm 配置",
            "cloudflare": "Cloudflare 配置",
            "yyds": "YYDS 配置",
            "mailnest": "MailNest 配置",
            "cloudmail": "CloudMail 配置",
        }
        self.provider_frame.configure(text=titles.get(provider, "邮箱服务商配置"))
        for widgets in self._provider_widget_groups.values():
            for widget in widgets:
                widget.grid_remove()
        for widget in self._provider_widget_groups.get(provider, self._cloudflare_widgets):
            # grid_remove 后无参 grid() 会恢复原行列
            widget.grid()

    def _refresh_cpa_fields(self):
        """未开启 SSO→auth 时隐藏 CPA 目录/远程配置。"""
        enabled = bool(self.cpa_auto_add_var.get())
        for widget in getattr(self, "_cpa_detail_widgets", []):
            if enabled:
                widget.grid()
            else:
                widget.grid_remove()

    def _refresh_sub2api_fields(self):
        """未开启 SSO→Sub2API 时隐藏 Sub2API 目录/远程配置。"""
        enabled = bool(self.sub2api_auto_add_var.get())
        for widget in getattr(self, "_sub2api_detail_widgets", []):
            if enabled:
                widget.grid()
            else:
                widget.grid_remove()

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(line, flush=True)
        self.log_text.insert(tk.END, f"{line}\n")
        self.log_text.see(tk.END)

    def clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def update_stats(self):
        fail_detail = format_fail_stats(getattr(self, "fail_stats", {}) or {})
        if self.fail_count:
            self.stats_var.set(
                f"成功: {self.success_count} | 失败: {self.fail_count}（{fail_detail}）"
            )
        else:
            self.stats_var.set(f"成功: {self.success_count} | 失败: 0")

    def _record_failure(self, exc):
        kind = classify_failure(exc)
        lock = getattr(self, "_stats_lock", None)
        if lock:
            with lock:
                self.fail_count += 1
                if not hasattr(self, "fail_stats") or self.fail_stats is None:
                    self.fail_stats = empty_fail_stats()
                self.fail_stats[kind] = self.fail_stats.get(kind, 0) + 1
        else:
            self.fail_count += 1
            if not hasattr(self, "fail_stats") or self.fail_stats is None:
                self.fail_stats = empty_fail_stats()
            self.fail_stats[kind] = self.fail_stats.get(kind, 0) + 1
        return kind

    def _record_success(self):
        lock = getattr(self, "_stats_lock", None)
        if lock:
            with lock:
                self.success_count += 1
        else:
            self.success_count += 1

    def _set_running_ui(self, running):
        self.is_running = running
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("运行中..." if running else "就绪")
        self.status_label.config(foreground="blue" if running else "green")

    def should_stop(self):
        return self.stop_requested or not self.is_running

    def start_registration(self):
        if self.is_running:
            self.log("[!] 当前已有任务在运行")
            return

        config["email_provider"] = self.email_provider_var.get().strip() or "cloudflare"
        config["enable_nsfw"] = bool(self.nsfw_var.get())
        config["proxy"] = self.proxy_var.get().strip()
        config["duckmail_api_key"] = self.api_key_var.get().strip()
        config["duckmail_api_base"] = self.duckmail_api_base_var.get().strip() or DUCKMAIL_API_BASE_DEFAULT
        config["cloudflare_api_base"] = self.cloudflare_api_base_var.get().strip()
        config["cloudflare_api_key"] = self.cloudflare_api_key_var.get().strip()
        config["cloudflare_auth_mode"] = self.cloudflare_auth_mode_var.get().strip() or "none"
        config["defaultDomains"] = self.default_domains_var.get().strip()
        config["cloudflare_custom_auth"] = self.cloudflare_custom_auth_var.get().strip()
        config["yyds_api_key"] = self.yyds_api_key_var.get().strip()
        config["yyds_jwt"] = self.yyds_jwt_var.get().strip()
        config["mailnest_api_key"] = self.mailnest_api_key_var.get().strip()
        config["mailnest_project_code"] = (
            self.mailnest_project_code_var.get().strip() or MAILNEST_DEFAULT_PROJECT_CODE
        )
        config["yyds_default_domain"] = self.yyds_default_domain_var.get().strip()
        config["cloudmail_url"] = self.cloudmail_url_var.get().strip()
        config["cloudmail_admin_email"] = self.cloudmail_admin_email_var.get().strip()
        config["cloudmail_password"] = self.cloudmail_password_var.get()
        config["cpa_auto_add"] = bool(self.cpa_auto_add_var.get())
        config["cpa_auth_dir"] = self.cpa_auth_dir_var.get().strip()
        config["cpa_remote_url"] = self.cpa_remote_url_var.get().strip()
        config["cpa_management_key"] = self.cpa_management_key_var.get().strip()
        config["sub2api_auto_add"] = bool(self.sub2api_auto_add_var.get())
        config["sub2api_dir"] = self.sub2api_dir_var.get().strip()
        config["sub2api_url"] = self.sub2api_url_var.get().strip()
        config["sub2api_token"] = self.sub2api_token_var.get().strip()
        try:
            config["sub2api_batch_size"] = max(int(self.sub2api_batch_size_var.get()), 1)
        except (TypeError, ValueError):
            config["sub2api_batch_size"] = 20
        config["sub2api_verify"] = bool(self.sub2api_verify_var.get())
        raw_paths = [x.strip() for x in self.cloudflare_paths_var.get().split(",") if x.strip()]
        if len(raw_paths) >= 4:
            config["cloudflare_path_domains"] = raw_paths[0] if raw_paths[0].startswith("/") else ("/" + raw_paths[0])
            config["cloudflare_path_accounts"] = raw_paths[1] if raw_paths[1].startswith("/") else ("/" + raw_paths[1])
            config["cloudflare_path_token"] = raw_paths[2] if raw_paths[2].startswith("/") else ("/" + raw_paths[2])
            config["cloudflare_path_messages"] = raw_paths[3] if raw_paths[3].startswith("/") else ("/" + raw_paths[3])
        save_config()
        if config["email_provider"] == "cloudflare" and not config["cloudflare_api_base"]:
            self.log("[!] Cloudflare 模式需要先填写 Cloudflare API Base")
            return
        if config["email_provider"] == "mailnest" and not config["mailnest_api_key"]:
            self.log("[!] MailNest 模式需要先填写 MailNest API Key")
            return
        if config["email_provider"] == "cloudmail":
            missing = []
            if not get_cloudmail_url():
                missing.append("CloudMail URL")
            if not get_cloudmail_admin_email():
                missing.append("CloudMail 管理员邮箱")
            if not get_cloudmail_password():
                missing.append("CloudMail 管理员密码")
            if not config["defaultDomains"]:
                missing.append("默认收信域名")
            if missing:
                self.log(f"[!] CloudMail 模式缺少配置: {', '.join(missing)}")
                return
        if config.get("cpa_auto_add") and not config.get("cpa_auth_dir") and not config.get("cpa_remote_url"):
            self.log("[!] 已开启 SSO→auth，但未配置 auth 目录或远程地址")
            return
        try:
            count = int(self.count_var.get())
        except Exception:
            self.log("[!] 注册数量无效")
            return
        try:
            workers = int(self.workers_var.get())
        except Exception:
            workers = 1
        workers = max(1, min(workers, 8, count))
        config["register_count"] = count
        config["register_workers"] = workers
        save_config()
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.fail_stats = empty_fail_stats()
        self.results = []
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.accounts_output_file = os.path.join(
            os.path.dirname(__file__), f"accounts_{now}.txt"
        )
        self.update_stats()
        self._set_running_ui(True)
        self._stats_lock = threading.Lock()
        self._accounts_lock = threading.Lock()
        self.log(f"[*] 配置已保存，开始执行。目标数量: {count} | 并发: {workers}")
        if int(self.workers_var.get() or 1) > count:
            self.log(f"[*] 并发已自动调整为 {workers}（不超过注册数量）")
        self.log(f"[*] SSO→auth: {'开' if config.get('cpa_auto_add') else '关（仅保存 SSO）'}")
        self.log(f"[*] SSO→Sub2API: {'开' if config.get('sub2api_auto_add') else '关'}")
        self.log(f"[*] 成功账号将实时保存到: {self.accounts_output_file}")
        threading.Thread(
            target=self._run_registration_entry,
            args=(count, workers),
            daemon=True,
        ).start()

    def stop_registration(self):
        self.stop_requested = True
        self.log("[!] 用户停止注册")

    def _run_registration_entry(self, count, workers):
        # 并发数不超过任务数，避免空 worker 白开浏览器
        workers = max(1, min(int(workers or 1), 8, int(count or 1)))
        # 启动 Sub2API 批次会话（共享写入器 + 并行验活线程池），供各 worker 复用
        begin_sub2api_batch_session(log_callback=self.log)
        try:
            if workers <= 1:
                self.run_registration(count, worker_id=0, workers=1)
            else:
                base, rem = divmod(count, workers)
                chunks = [base + (1 if i < rem else 0) for i in range(workers)]
                # 去掉 0 任务分片，重新编号
                chunks = [n for n in chunks if n > 0]
                self.log(f"[*] 实际并发 worker={len(chunks)}，分片={chunks}")
                threads = []
                for wid, n in enumerate(chunks):
                    t = threading.Thread(
                        target=self.run_registration,
                        args=(n, wid, len(chunks)),
                        daemon=True,
                    )
                    t.start()
                    threads.append(t)
                    # 错开启动，降低同时拉起 Chrome 的冲突
                    time.sleep(0.8)
                for t in threads:
                    t.join()
        finally:
            # 等待 Sub2API 并行验活/导出任务收尾（关闭共享线程池）
            try:
                wait_sub2api_pending(log_callback=self.log)
            except BaseException:
                pass
            # 协调线程自身无浏览器；各 worker 线程 finally 已各自 stop
            self._set_running_ui(False)
            self.log(
                f"[*] 任务结束。成功 {self.success_count} | 失败 {self.fail_count}"
                + (f" | {format_fail_stats(self.fail_stats)}" if self.fail_count else "")
            )

    def run_registration(self, count, worker_id=0, workers=1):
        prefix = f"[W{worker_id + 1}] " if workers > 1 else ""

        def wlog(message):
            text = str(message)
            if prefix and not text.startswith(prefix):
                self.log(prefix + text)
            else:
                self.log(text)

        try:
            start_browser(log_callback=wlog)
            wlog("[*] 浏览器已启动")
            i = 0
            retry_count_for_slot = 0
            max_slot_retry = 3
            while i < count:
                if self.should_stop():
                    break
                wlog(f"--- 开始第 {i + 1}/{count} 个账号 ---")
                try:
                    email = ""
                    dev_token = ""
                    code = ""
                    mail_ok = False
                    max_mail_retry = 3
                    for mail_try in range(1, max_mail_retry + 1):
                        wlog(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
                        open_signup_page(
                            log_callback=wlog, cancel_callback=self.should_stop
                        )
                        wlog("[*] 2. 创建邮箱并提交")
                        email, dev_token = fill_email_and_submit(
                            log_callback=wlog, cancel_callback=self.should_stop
                        )
                        wlog(f"[*] 邮箱: {email}")
                        wlog(f"[Debug] 邮箱credential(jwt): {dev_token}")
                        try:
                            with open(
                                os.path.join(os.path.dirname(__file__), "mail_credentials.txt"),
                                "a",
                                encoding="utf-8",
                            ) as f:
                                f.write(f"{email}\t{dev_token}\n")
                        except Exception:
                            pass
                        wlog("[*] 3. 拉取验证码")
                        try:
                            code = fill_code_and_submit(
                                email,
                                dev_token,
                                log_callback=wlog,
                                cancel_callback=self.should_stop,
                            )
                            mail_ok = True
                            break
                        except Exception as mail_exc:
                            msg = str(mail_exc)
                            if ("未收到验证码" in msg or "验证码" in msg) and mail_try < max_mail_retry:
                                wlog(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                                restart_browser(log_callback=wlog)
                                sleep_with_cancel(1, self.should_stop)
                                continue
                            raise

                    if not mail_ok:
                        raise Exception("验证码阶段失败，已达到最大重试次数")
                    wlog(f"[*] 验证码: {code}")
                    wlog("[*] 4. 填写资料")
                    profile = fill_profile_and_submit(
                        log_callback=wlog, cancel_callback=self.should_stop
                    )
                    wlog(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
                    wlog("[*] 5. 等待 sso cookie")
                    sso = wait_for_sso_cookie(
                        log_callback=wlog, cancel_callback=self.should_stop
                    )
                    if config.get("enable_nsfw", True):
                        wlog("[*] 6. 开启 NSFW")
                        cf_clearance, browser_ua = extract_cf_clearance_and_ua(wlog)
                        nsfw_ok, nsfw_msg = enable_nsfw_for_token(
                            sso, cf_clearance=cf_clearance, user_agent=browser_ua, log_callback=wlog
                        )
                        if nsfw_ok:
                            wlog(f"[+] NSFW 开启成功: {nsfw_msg}")
                        else:
                            wlog(f"[!] NSFW 未开启，继续保存账号: {nsfw_msg}")
                    lock = getattr(self, "_stats_lock", None)
                    export_ok = wait_sub2api_account_result(
                        add_sso_to_sub2api(sso, email=email, log_callback=wlog)
                    )
                    if not export_ok:
                        raise Exception("换 token 或验活失败，不计入成功")
                    add_sso_to_cpa(sso, email=email, log_callback=wlog)
                    if lock:
                        with lock:
                            self.results.append({"email": email, "sso": sso, "profile": profile})
                    else:
                        self.results.append({"email": email, "sso": sso, "profile": profile})
                    try:
                        line = f"{email}----{profile.get('password','')}----{sso}\n"
                        alock = getattr(self, "_accounts_lock", None)
                        if alock:
                            with alock:
                                with open(self.accounts_output_file, "a", encoding="utf-8") as f:
                                    f.write(line)
                        else:
                            with open(self.accounts_output_file, "a", encoding="utf-8") as f:
                                f.write(line)
                    except Exception as file_exc:
                        wlog(f"[Debug] 保存账号文件失败: {file_exc}")
                    self._record_success()
                    retry_count_for_slot = 0
                    i += 1
                    wlog(f"[+] 验活成功并计入: {email}")
                    if (
                        self.success_count > 0
                        and self.success_count % MEMORY_CLEANUP_INTERVAL == 0
                        and i < count
                        and workers <= 1
                    ):
                        cleanup_runtime_memory(
                            log_callback=wlog,
                            reason=f"已成功 {self.success_count} 个账号，执行定期清理",
                        )
                except RegistrationCancelled:
                    wlog("[!] 注册被用户停止")
                    break
                except EmailDomainRejected as exc:
                    kind = self._record_failure(exc)
                    retry_count_for_slot = 0
                    i += 1
                    wlog(f"[-] 邮箱域名被 xAI 拒绝 [{FAIL_LABELS.get(kind, kind)}]: {exc}")
                    wlog("[!] 请更换邮箱提供商或域名（如 Cloudflare 自建域 / MailNest），公共临时域常被拉黑")
                except AccountRetryNeeded as exc:
                    retry_count_for_slot += 1
                    if retry_count_for_slot <= max_slot_retry:
                        wlog(
                            f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                        )
                    else:
                        kind = self._record_failure(exc)
                        wlog(
                            f"[-] 当前账号已达到最大重试次数，跳过 [{FAIL_LABELS.get(kind, kind)}]: {exc}"
                        )
                        retry_count_for_slot = 0
                        i += 1
                except Exception as exc:
                    kind = self._record_failure(exc)
                    retry_count_for_slot = 0
                    i += 1
                    wlog(f"[-] 注册失败 [{FAIL_LABELS.get(kind, kind)}]: {exc}")
                finally:
                    self.update_stats()
                    if self.should_stop():
                        break
                    # 每轮结束只关浏览器，不立刻再开。
                    # 下一轮 open_signup_page 会按需启动并导航到官网，避免空浏览器残留。
                    if i >= count:
                        continue
                    try:
                        stop_browser()
                        time.sleep(0.5)
                    except Exception as close_exc:
                        if self.should_stop():
                            break
                        wlog(f"[Debug] 轮次关闭浏览器失败: {close_exc}")
        except RegistrationCancelled:
            wlog("[!] 注册被用户停止")
        except Exception as exc:
            wlog(f"[!] 任务异常: {exc}")
        finally:
            try:
                stop_browser()
            except BaseException:
                pass
            # 多 worker 时由 _run_registration_entry 统一收尾 UI
            if workers <= 1:
                self._set_running_ui(False)
                self.log(
                    f"[*] 任务结束。成功 {self.success_count} | 失败 {self.fail_count}"
                    + (f" | {format_fail_stats(self.fail_stats)}" if self.fail_count else "")
                )


class CliStopController:
    def __init__(self):
        self.stop_requested = False

    def should_stop(self):
        return self.stop_requested

    def stop(self):
        self.stop_requested = True


def cli_log(message):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def run_registration_cli(count):
    controller = CliStopController()

    # 一次 Ctrl+C 可靠置停：SIGINT 处理器直接设停止标志，不依赖异常在
    # curl_cffi C 回调里向上传播（那里 KeyboardInterrupt 会被吞掉，导致
    # 第一次 Ctrl+C 无效、循环继续跑下一个账号）。连按两次 Ctrl+C 时第二次
    # 恢复默认行为强制中断。
    _prev_sigint = signal.getsignal(signal.SIGINT)

    def _on_sigint(signum, frame):
        if controller.should_stop():
            # 第二次：恢复默认并重新抛出，强制中断
            signal.signal(signal.SIGINT, _prev_sigint)
            raise KeyboardInterrupt
        controller.stop()
        cli_log("[!] 收到 Ctrl+C，正在停止（再按一次强制中断）")

    signal.signal(signal.SIGINT, _on_sigint)
    success_count = 0
    fail_count = 0
    fail_stats = empty_fail_stats()
    retry_count_for_slot = 0
    max_slot_retry = 3
    accounts_output_file = os.path.join(
        os.path.dirname(__file__),
        f"accounts_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
    )
    workers = max(1, min(int(config.get("register_workers", 1) or 1), 8, int(count or 1)))
    cli_log(f"[*] 终端模式启动，目标数量: {count} | 并发: {workers}")
    cli_log(f"[*] SSO→auth: {'开' if config.get('cpa_auto_add') else '关（仅保存 SSO）'}")
    cli_log(f"[*] SSO→Sub2API: {'开' if config.get('sub2api_auto_add') else '关'}")
    cli_log(f"[*] 成功账号将实时保存到: {accounts_output_file}")
    begin_sub2api_batch_session(log_callback=cli_log)

    def _cli_record_failure(exc):
        nonlocal fail_count
        kind = classify_failure(exc)
        fail_count += 1
        fail_stats[kind] = fail_stats.get(kind, 0) + 1
        return kind

    if workers > 1:
        # CLI 并发：多线程，每线程独立浏览器（thread-local）
        stats_lock = threading.Lock()
        accounts_lock = threading.Lock()
        base, rem = divmod(count, workers)
        chunks = [base + (1 if i < rem else 0) for i in range(workers)]
        threads = []
        shared = {"success": 0, "fail": 0, "fail_stats": empty_fail_stats()}

        def worker(n, wid):
            local_success = 0
            local_fail = 0
            local_fail_stats = empty_fail_stats()
            try:
                start_browser(log_callback=lambda m: cli_log(f"[W{wid+1}] {m}"))
                i = 0
                retry = 0
                while i < n and not controller.should_stop():
                    try:
                        open_signup_page(
                            log_callback=lambda m: cli_log(f"[W{wid+1}] {m}"),
                            cancel_callback=controller.should_stop,
                        )
                        email, dev_token = fill_email_and_submit(
                            log_callback=lambda m: cli_log(f"[W{wid+1}] {m}"),
                            cancel_callback=controller.should_stop,
                        )
                        code = fill_code_and_submit(
                            email,
                            dev_token,
                            log_callback=lambda m: cli_log(f"[W{wid+1}] {m}"),
                            cancel_callback=controller.should_stop,
                        )
                        profile = fill_profile_and_submit(
                            log_callback=lambda m: cli_log(f"[W{wid+1}] {m}"),
                            cancel_callback=controller.should_stop,
                        )
                        sso = wait_for_sso_cookie(
                            log_callback=lambda m: cli_log(f"[W{wid+1}] {m}"),
                            cancel_callback=controller.should_stop,
                        )
                        if config.get("enable_nsfw", True):
                            cf_clearance, browser_ua = extract_cf_clearance_and_ua(
                                log_callback=lambda m: cli_log(f"[W{wid+1}] {m}")
                            )
                            enable_nsfw_for_token(
                                sso,
                                cf_clearance=cf_clearance,
                                user_agent=browser_ua,
                                log_callback=lambda m: cli_log(f"[W{wid+1}] {m}"),
                            )
                        line = f"{email}----{profile.get('password','')}----{sso}\n"
                        export_ok = wait_sub2api_account_result(
                            add_sso_to_sub2api(
                                sso,
                                email=email,
                                log_callback=lambda m: cli_log(f"[W{wid+1}] {m}"),
                            )
                        )
                        if not export_ok:
                            raise Exception("换 token 或验活失败，不计入成功")
                        add_sso_to_cpa(sso, email=email, log_callback=lambda m: cli_log(f"[W{wid+1}] {m}"))
                        with accounts_lock:
                            with open(accounts_output_file, "a", encoding="utf-8") as f:
                                f.write(line)
                        local_success += 1
                        i += 1
                        retry = 0
                        cli_log(f"[W{wid+1}] [+] 验活成功并计入: {email}")
                    except RegistrationCancelled:
                        break
                    except EmailDomainRejected as exc:
                        kind = classify_failure(exc)
                        local_fail_stats[kind] = local_fail_stats.get(kind, 0) + 1
                        local_fail += 1
                        i += 1
                        retry = 0
                        cli_log(f"[W{wid+1}] [-] 域名拒绝: {exc}")
                    except AccountRetryNeeded as exc:
                        retry += 1
                        if retry > max_slot_retry:
                            kind = classify_failure(exc)
                            local_fail_stats[kind] = local_fail_stats.get(kind, 0) + 1
                            local_fail += 1
                            i += 1
                            retry = 0
                            cli_log(f"[W{wid+1}] [-] 卡住跳过: {exc}")
                    except Exception as exc:
                        kind = classify_failure(exc)
                        local_fail_stats[kind] = local_fail_stats.get(kind, 0) + 1
                        local_fail += 1
                        i += 1
                        retry = 0
                        cli_log(f"[W{wid+1}] [-] 失败 [{FAIL_LABELS.get(kind, kind)}]: {exc}")
                    finally:
                        if i < n and not controller.should_stop():
                            try:
                                stop_browser()
                                time.sleep(0.3)
                            except Exception:
                                pass
            finally:
                try:
                    stop_browser()
                except Exception:
                    pass
                with stats_lock:
                    shared["success"] += local_success
                    shared["fail"] += local_fail
                    for k, v in local_fail_stats.items():
                        shared["fail_stats"][k] = shared["fail_stats"].get(k, 0) + v

        for wid, n in enumerate(chunks):
            if n <= 0:
                continue
            t = threading.Thread(target=worker, args=(n, wid), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        try:
            wait_sub2api_pending(log_callback=cli_log)
        except BaseException:
            pass
        success_count = shared["success"]
        fail_count = shared["fail"]
        fail_stats = shared["fail_stats"]
        cli_log(
            f"[*] 任务结束。成功 {success_count} | 失败 {fail_count}"
            + (f" | {format_fail_stats(fail_stats)}" if fail_count else "")
        )
        return

    try:
        start_browser(log_callback=cli_log)
        cli_log("[*] 浏览器已启动")
        i = 0
        while i < count:
            if controller.should_stop():
                break
            cli_log(f"--- 开始第 {i + 1}/{count} 个账号 ---")
            try:
                email = ""
                dev_token = ""
                code = ""
                mail_ok = False
                max_mail_retry = 3
                for mail_try in range(1, max_mail_retry + 1):
                    cli_log(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
                    open_signup_page(
                        log_callback=cli_log, cancel_callback=controller.should_stop
                    )
                    cli_log("[*] 2. 创建邮箱并提交")
                    email, dev_token = fill_email_and_submit(
                        log_callback=cli_log, cancel_callback=controller.should_stop
                    )
                    cli_log(f"[*] 邮箱: {email}")
                    cli_log(f"[Debug] 邮箱credential(jwt): {dev_token}")
                    try:
                        with open(
                            os.path.join(os.path.dirname(__file__), "mail_credentials.txt"),
                            "a",
                            encoding="utf-8",
                        ) as f:
                            f.write(f"{email}\t{dev_token}\n")
                    except Exception:
                        pass
                    cli_log("[*] 3. 拉取验证码")
                    try:
                        code = fill_code_and_submit(
                            email,
                            dev_token,
                            log_callback=cli_log,
                            cancel_callback=controller.should_stop,
                        )
                        mail_ok = True
                        break
                    except Exception as mail_exc:
                        msg = str(mail_exc)
                        if ("未收到验证码" in msg or "验证码" in msg) and mail_try < max_mail_retry:
                            cli_log(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                            restart_browser(log_callback=cli_log)
                            sleep_with_cancel(1, controller.should_stop)
                            continue
                        raise

                if not mail_ok:
                    raise Exception("验证码阶段失败，已达到最大重试次数")
                cli_log(f"[*] 验证码: {code}")
                cli_log("[*] 4. 填写资料")
                profile = fill_profile_and_submit(
                    log_callback=cli_log, cancel_callback=controller.should_stop
                )
                cli_log(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
                cli_log("[*] 5. 等待 sso cookie")
                sso = wait_for_sso_cookie(
                    log_callback=cli_log, cancel_callback=controller.should_stop
                )
                if config.get("enable_nsfw", True):
                    cli_log("[*] 6. 开启 NSFW")
                    cf_clearance, browser_ua = extract_cf_clearance_and_ua(log_callback=cli_log)
                    nsfw_ok, nsfw_msg = enable_nsfw_for_token(
                        sso, cf_clearance=cf_clearance, user_agent=browser_ua, log_callback=cli_log
                    )
                    if nsfw_ok:
                        cli_log(f"[+] NSFW 开启成功: {nsfw_msg}")
                    else:
                        cli_log(f"[!] NSFW 未开启，继续保存账号: {nsfw_msg}")
                try:
                    line = f"{email}----{profile.get('password','')}----{sso}\n"
                except Exception:
                    line = f"{email}--------{sso}\n"
                export_ok = wait_sub2api_account_result(
                    add_sso_to_sub2api(sso, email=email, log_callback=cli_log)
                )
                if not export_ok:
                    raise Exception("换 token 或验活失败，不计入成功")
                add_sso_to_cpa(sso, email=email, log_callback=cli_log)
                try:
                    with open(accounts_output_file, "a", encoding="utf-8") as f:
                        f.write(line)
                except Exception as file_exc:
                    cli_log(f"[Debug] 保存账号文件失败: {file_exc}")
                success_count += 1
                retry_count_for_slot = 0
                i += 1
                cli_log(f"[+] 验活成功并计入: {email}")
                cli_log(f"[*] 当前统计: 成功 {success_count} | 失败 {fail_count}")
                if success_count > 0 and success_count % MEMORY_CLEANUP_INTERVAL == 0 and i < count:
                    cleanup_runtime_memory(
                        log_callback=cli_log,
                        reason=f"已成功 {success_count} 个账号，执行定期清理",
                    )
            except RegistrationCancelled:
                cli_log("[!] 注册被停止")
                break
            except EmailDomainRejected as exc:
                kind = _cli_record_failure(exc)
                retry_count_for_slot = 0
                i += 1
                cli_log(f"[-] 邮箱域名被 xAI 拒绝 [{FAIL_LABELS.get(kind, kind)}]: {exc}")
                cli_log("[!] 请更换邮箱提供商或域名（如 Cloudflare 自建域 / MailNest），公共临时域常被拉黑")
            except AccountRetryNeeded as exc:
                retry_count_for_slot += 1
                if retry_count_for_slot <= max_slot_retry:
                    cli_log(
                        f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                    )
                else:
                    kind = _cli_record_failure(exc)
                    retry_count_for_slot = 0
                    i += 1
                    cli_log(f"[-] 当前账号已达到最大重试次数，跳过 [{FAIL_LABELS.get(kind, kind)}]: {exc}")
            except Exception as exc:
                kind = _cli_record_failure(exc)
                retry_count_for_slot = 0
                i += 1
                cli_log(f"[-] 注册失败 [{FAIL_LABELS.get(kind, kind)}]: {exc}")
            finally:
                if controller.should_stop():
                    break
                # 每轮结束只关浏览器，不立刻再开。
                # 下一轮 open_signup_page 会按需启动并导航到官网，避免空浏览器残留。
                if i >= count:
                    continue
                try:
                    stop_browser()
                    time.sleep(0.5)
                except KeyboardInterrupt:
                    controller.stop()
                    cli_log("[!] 收到 Ctrl+C，正在停止（再按一次强制中断）")
                    break
                except RegistrationCancelled:
                    break
                except Exception as close_exc:
                    if controller.should_stop():
                        break
                    cli_log(f"[Debug] 轮次关闭浏览器失败: {close_exc}")
    except KeyboardInterrupt:
        controller.stop()
        cli_log("[!] 收到 Ctrl+C，正在停止并清理")
    except RegistrationCancelled:
        cli_log("[!] 注册被停止")
    except Exception as exc:
        cli_log(f"[!] 任务异常: {exc}")
    finally:
        try:
            wait_sub2api_pending(log_callback=cli_log)
        except BaseException:
            pass
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
        try:
            cleanup_runtime_memory(log_callback=cli_log, reason="任务结束")
        except BaseException:
            pass
        try:
            cli_log(
                f"[*] 任务结束。成功 {success_count} | 失败 {fail_count}"
                + (f" | {format_fail_stats(fail_stats)}" if fail_count else "")
            )
        except BaseException:
            pass
        try:
            signal.signal(signal.SIGINT, _prev_sigint)
        except Exception:
            pass


def main_cli():
    load_config()
    count = int(config.get("register_count", 1) or 1)
    cli_log("[*] CLI 已加载配置")
    cli_log(f"[*] 当前邮箱服务商: {config.get('email_provider', 'duckmail')} | 注册数量: {count}")
    cli_log("[*] 输入 start 后开始；按 Ctrl+C 可强制停止")
    try:
        command = input("> ").strip().lower()
    except KeyboardInterrupt:
        cli_log("[!] 已取消")
        return
    if command != "start":
        cli_log("[!] 未输入 start，已退出")
        return
    try:
        run_registration_cli(count)
    except KeyboardInterrupt:
        # 清理阶段仍可能漏出，保证 CLI 干净退出
        cli_log("[!] 已停止")


def main():
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in ("start", "cli", "--cli"):
        main_cli()
        return
    root = tk.Tk()
    setup_light_theme(root)
    app = GrokRegisterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
