#!/usr/bin/env python3
"""
SSO cookie → ~/.grok/auth.json 格式（纯 HTTP Device Flow）

用法:
  # 单个 / 批量 SSO，写出多个独立 auth 文件（每个可直接 cp 到 ~/.grok/auth.json）
  python3 sso_to_auth_json.py --sso sso_list.txt --out-dir ./auth_out

  # 合并到一个 json（key 带 user_id 后缀，避免覆盖）
  python3 sso_to_auth_json.py --sso sso_list.txt --out auth_merged.json --merge

  # 单行 sso
  python3 sso_to_auth_json.py --sso-cookie 'eyJ...' --out ~/.grok/auth.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_ISSUER = "https://auth.x.ai"
AUTH_KEY = f"{OIDC_ISSUER}::{CLIENT_ID}"
SCOPES = (
    "openid profile email offline_access grok-cli:access "
    "api:access conversations:read conversations:write"
)

# --- CLIProxyAPI (CPA) 扁平格式常量 ------------------------------------------
# 与上游 grokRegister-cpa 原生导出保持一致：
# Build/CLI token（scope 含 grok-cli:access）走 cli-chat-proxy.grok.com，
# 并附带 grok-cli headers（见 token_to_cpa_record）。
CPA_TOKEN_ENDPOINT = f"{OIDC_ISSUER}/oauth2/token"
CPA_GROK_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CPA_GROK_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell",
}

# --- Sub2API (Wei-Shaw/sub2api) Grok OAuth credentials ----------------------
# 对齐 backend/internal/service/grok_oauth_service.go BuildAccountCredentials
SUB2API_CLIENT_ID = CLIENT_ID
SUB2API_SCOPE = "openid profile email offline_access grok-cli:access api:access"
SUB2API_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
# 与 Sub2API v0.1.152 / CPA 原生导出保持一致，避免上游 426 version(none)
SUB2API_VERIFY_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell",
    "Accept": "application/json",
}


def verify_grok_credentials(
    creds: dict,
    proxy: str = "",
    timeout: int = 25,
) -> tuple[bool, str]:
    """对 cli-chat-proxy 发起一次 /models 验活。

    返回 (ok, message)。2xx/3xx 视为通过；401/403/426 等视为失败。
    """
    access = str((creds or {}).get("access_token") or "").strip()
    if not access:
        return False, "missing access_token"
    base = str((creds or {}).get("base_url") or SUB2API_BASE_URL).strip().rstrip("/")
    url = f"{base}/models"
    headers = dict(SUB2API_VERIFY_HEADERS)
    headers["Authorization"] = f"Bearer {access}"
    kwargs = {
        "headers": headers,
        "timeout": timeout,
        "impersonate": "chrome",
    }
    proxy = str(proxy or "").strip()
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    try:
        resp = requests.get(url, **kwargs)
    except Exception as exc:
        return False, f"request_error: {exc}"
    status = int(getattr(resp, "status_code", 0) or 0)
    body = (getattr(resp, "text", "") or "").strip().replace("\n", " ")
    if len(body) > 160:
        body = body[:160] + "..."
    if 200 <= status < 400:
        return True, f"HTTP {status}"
    detail = f"HTTP {status}"
    if body:
        detail = f"{detail}: {body}"
    return False, detail


def b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def decode_jwt_payload(token: str) -> dict:
    try:
        return json.loads(b64url_decode(token.split(".")[1]))
    except Exception:
        return {}


def rfc3339_ns(ts: float | None = None) -> str:
    """2026-07-10T01:00:00.000000000Z"""
    if ts is None:
        ts = time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"


def _urlopen(req, proxy: str = "", timeout: int = 15):
    """urllib 请求，proxy 非空时走代理。"""
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def request_device_code(proxy: str = "", log=print) -> dict | None:
    data = urllib.parse.urlencode({"client_id": CLIENT_ID, "scope": SCOPES}).encode()
    req = urllib.request.Request(
        f"{OIDC_ISSUER}/oauth2/device/code",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with _urlopen(req, proxy=proxy, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log(f"  [x] device/code HTTP {e.code}: {e.read().decode()[:200]}")
        return None


def poll_token(device_code: str, interval: int, expires_in: int, timeout: int = 60, proxy: str = "", log=print) -> dict | None:
    deadline = time.time() + min(expires_in, timeout)
    while time.time() < deadline:
        time.sleep(interval)
        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": device_code,
            }
        ).encode()
        req = urllib.request.Request(
            f"{OIDC_ISSUER}/oauth2/token",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with _urlopen(req, proxy=proxy, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = json.loads(e.read())
            error = err.get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            log(f"  [x] token: {error}")
            return None
    log("  [x] 轮询超时")
    return None


def sso_to_token(sso_cookie: str, proxy: str = "", log=print) -> dict | None:
    """SSO cookie → token dict (access/refresh/expires_in)。proxy 非空时全程走代理。"""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    s = requests.Session()
    if proxies:
        s.proxies = proxies
    s.cookies.set("sso", sso_cookie, domain=".x.ai")

    try:
        r = s.get("https://accounts.x.ai/", impersonate="chrome", timeout=15)
    except Exception as e:
        log(f"  [x] 网络错误: {e}")
        return None
    if "sign-in" in r.url or "sign-up" in r.url:
        log("  [x] sso 无效")
        return None
    log("  [ok] sso 有效")

    log("  [*] Device Flow...")
    dc = request_device_code(proxy=proxy, log=log)
    if not dc:
        return None
    log(f"  [*] user_code: {dc.get('user_code')}")

    try:
        s.get(dc["verification_uri_complete"], impersonate="chrome", timeout=15)
        r = s.post(
            f"{OIDC_ISSUER}/oauth2/device/verify",
            data={"user_code": dc["user_code"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=15,
            allow_redirects=True,
        )
        if "consent" not in r.url:
            log(f"  [x] verify 失败: {r.url}")
            return None
    except Exception as e:
        log(f"  [x] verify 异常: {e}")
        return None

    try:
        r = s.post(
            f"{OIDC_ISSUER}/oauth2/device/approve",
            data={
                "user_code": dc["user_code"],
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=15,
            allow_redirects=True,
        )
        if "done" not in r.url:
            log(f"  [x] approve 失败: {r.url}")
            return None
        log("  [ok] 授权确认")
    except Exception as e:
        log(f"  [x] approve 异常: {e}")
        return None

    token = poll_token(
        dc["device_code"],
        dc.get("interval", 5),
        dc.get("expires_in", 1800),
        proxy=proxy,
        log=log,
    )
    if not token:
        return None
    log(
        f"  [ok] access_token (expires_in={token.get('expires_in')}s)"
        + (" + refresh_token" if token.get("refresh_token") else "")
    )
    return token


def token_to_auth_entry(token: dict, email: str = "") -> tuple[str, dict]:
    """
    返回 (top_level_key, entry)
    top_level_key 固定为 issuer::client_id（与 ~/.grok/auth.json 一致）
    """
    access = token.get("access_token") or token.get("key") or ""
    refresh = token.get("refresh_token") or ""
    payload = decode_jwt_payload(access)

    user_id = payload.get("sub") or payload.get("principal_id") or ""
    principal_id = payload.get("principal_id") or user_id
    principal_type = payload.get("principal_type") or "User"

    expires_in = int(token.get("expires_in") or 21600)
    # 优先用 JWT exp
    if "exp" in payload:
        expires_at = rfc3339_ns(float(payload["exp"]))
    else:
        expires_at = rfc3339_ns(time.time() + expires_in)

    iat = payload.get("iat")
    create_time = rfc3339_ns(float(iat) if iat else time.time())

    entry = {
        "key": access,
        "auth_mode": "oidc",
        "create_time": create_time,
        "user_id": user_id,
        "email": email or "",
        "principal_type": principal_type,
        "principal_id": principal_id,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "oidc_issuer": OIDC_ISSUER,
        "oidc_client_id": CLIENT_ID,
    }
    return AUTH_KEY, entry


def _iso_utc_from_unix(ts) -> str:
    """unix 秒 → CPA 认的 RFC3339（秒级，带 Z）。"""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _safe_email_for_filename(email: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-@" else "_" for ch in email)
    return safe or "unknown"


def token_to_cpa_record(token: dict, email: str = "") -> dict:
    """token dict → CLIProxyAPI 扁平 xai auth 记录。

    对齐上游 grokRegister-cpa 原生导出（cli-chat-proxy + grok-cli headers）。
    """
    access = token.get("access_token") or token.get("key") or ""
    refresh = token.get("refresh_token") or ""
    id_token = token.get("id_token") or ""
    payload = decode_jwt_payload(access)
    id_payload = decode_jwt_payload(id_token) if id_token else {}

    if not email:
        email = id_payload.get("email") or payload.get("email") or ""
    sub = payload.get("sub") or id_payload.get("sub") or ""

    # expired: 优先 access token 的 exp，其次 expires_in 推算
    expired = ""
    if "exp" in payload:
        expired = _iso_utc_from_unix(payload["exp"])
    elif token.get("expires_in") is not None:
        try:
            expired = _iso_utc_from_unix(int(time.time()) + int(token["expires_in"]))
        except Exception:
            expired = ""

    return {
        "type": "xai",
        "auth_kind": "oauth",
        "email": email or "",
        "sub": sub,
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "token_type": token.get("token_type", "Bearer"),
        "expires_in": token.get("expires_in", None),
        "expired": expired,
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "redirect_uri": "",
        "token_endpoint": CPA_TOKEN_ENDPOINT,
        "base_url": CPA_GROK_BASE_URL,
        "disabled": False,
        "headers": dict(CPA_GROK_HEADERS),
    }


def cpa_auth_filename(record: dict) -> str:
    """生成 CPA auth 文件名：xai-<email>.json。"""
    ident = str(record.get("email") or "").strip() or str(record.get("sub") or "").strip()
    safe = _safe_email_for_filename(ident)
    # 避免 email 本地部分已是 xai 时出现 "xai-xai..."
    fname = safe if safe.lower().startswith("xai") else f"xai-{safe}"
    return f"{fname}.json"


def write_cpa_auth(auth_dir: Path, record: dict) -> Path:
    """写出 CPA 可热加载的 xai-<email>.json（原子替换）。

    无 email 时用 sub(user_id) 命名，避免多个无 email 账号写成同一个
    xai-unknown.json 互相覆盖。
    """
    auth_dir.mkdir(parents=True, exist_ok=True)
    path = auth_dir / cpa_auth_filename(record)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def token_to_sub2api_credentials(token: dict, email: str = "") -> dict:
    """token dict → Sub2API Grok OAuth credentials（BuildAccountCredentials）。

    官方字段：access_token / refresh_token / token_type / expires_at / email /
    id_token / client_id / scope / base_url（cli-chat-proxy）。
    参考：https://github.com/Wei-Shaw/sub2api
    """
    access = token.get("access_token") or token.get("key") or ""
    refresh = token.get("refresh_token") or ""
    id_token = token.get("id_token") or ""
    payload = decode_jwt_payload(access)
    id_payload = decode_jwt_payload(id_token) if id_token else {}

    if not email:
        email = id_payload.get("email") or payload.get("email") or ""

    expires_at = ""
    if "exp" in payload:
        expires_at = _iso_utc_from_unix(payload["exp"])
    elif token.get("expires_in") is not None:
        try:
            expires_at = _iso_utc_from_unix(int(time.time()) + int(token["expires_in"]))
        except Exception:
            expires_at = ""
    if not expires_at:
        expires_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    creds: dict = {
        "access_token": access,
        "expires_at": expires_at,
        "base_url": SUB2API_BASE_URL,
        "client_id": SUB2API_CLIENT_ID,
        "scope": SUB2API_SCOPE,
    }
    if refresh:
        creds["refresh_token"] = refresh
    token_type = str(token.get("token_type") or "Bearer").strip()
    if token_type:
        creds["token_type"] = token_type
    if id_token:
        creds["id_token"] = id_token
    if email:
        creds["email"] = email
    return creds


SUB2API_DATA_TYPE = "sub2api-data"
SUB2API_DATA_VERSION = 1
SUB2API_IMPORT_BUNDLE_NAME = "sub2api_accounts_import.json"


def sub2api_auth_filename(creds: dict) -> str:
    """生成 Sub2API 导入文件名：grok-<email>.json。"""
    ident = str(creds.get("email") or "").strip()
    if not ident:
        payload = decode_jwt_payload(str(creds.get("access_token") or ""))
        ident = str(payload.get("sub") or "").strip() or "unknown"
    safe = _safe_email_for_filename(ident)
    fname = safe if safe.lower().startswith("grok") else f"grok-{safe}"
    return f"{fname}.json"


def _sub2api_expires_unix(creds: dict) -> int | None:
    """从 credentials.expires_at（ISO/unix）解析账号级 expires_at（unix 秒）。"""
    raw = creds.get("expires_at")
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip()
    if text.isdigit():
        return int(text)
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ):
        try:
            dt = datetime.strptime(text.replace("Z", "+0000") if fmt.endswith("%z") and text.endswith("Z") else text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    try:
        # RFC3339 fallback: 2026-07-13T07:56:53Z
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text[:-1]).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def build_sub2api_account_payload(
    creds: dict,
    name: str = "",
    concurrency: int = 1,
    priority: int = 50,
) -> dict:
    """组装单条 Sub2API 账号（Management API 创建 / 数据导入 accounts[] 共用）。"""
    email = str(creds.get("email") or "").strip()
    account_name = (name or email or "Grok OAuth Account").strip()
    account: dict = {
        "name": account_name,
        "platform": "grok",
        "type": "oauth",
        "credentials": creds,
        "concurrency": max(int(concurrency or 1), 1),
        "priority": int(priority if priority is not None else 50),
        "rate_multiplier": 1,
        # False：避免 access_token 到期后被踢出调度，导致 refresh_token 永远无法触发
        "auto_pause_on_expired": False,
    }
    expires_unix = _sub2api_expires_unix(creds)
    if expires_unix is not None:
        account["expires_at"] = expires_unix
    return account


def build_sub2api_data_payload(accounts: list, proxies: list | None = None) -> dict:
    """组装 Sub2API「导入数据」所需的导出包（type=sub2api-data）。"""
    return {
        "type": SUB2API_DATA_TYPE,
        "version": SUB2API_DATA_VERSION,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": list(proxies or []),
        "accounts": list(accounts or []),
    }


def _account_identity(account: dict) -> str:
    creds = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    email = str((creds or {}).get("email") or account.get("name") or "").strip().lower()
    return email


class Sub2APIBatchWriter:
    """按固定数量将账号实时写入独立的 Sub2API 导入包。"""

    def __init__(
        self,
        output_root: Path,
        batch_size: int = 20,
        session_name: str | None = None,
    ):
        self.output_root = Path(output_root)
        self.batch_size = max(int(batch_size or 20), 1)
        self.session_name = session_name or datetime.now().strftime(
            "batch_%Y%m%d_%H%M%S_%f"
        )
        self.session_dir = self.output_root / self.session_name
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self._packages: list[list[dict]] = []
        self._locations: dict[str, tuple[int, int]] = {}
        self.total_accounts = 0

    @property
    def package_count(self) -> int:
        return len(self._packages)

    def _package_path(self, package_index: int) -> Path:
        return self.session_dir / f"sub2api_accounts_{package_index + 1:03d}.json"

    def _write_package(self, package_index: int) -> Path:
        path = self._package_path(package_index)
        payload = build_sub2api_data_payload(self._packages[package_index])
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return path

    def add_credentials(self, creds: dict, name: str = "") -> dict:
        """加入一条账号并立即写盘；重复邮箱更新原位置，不增加计数。"""
        account = build_sub2api_account_payload(creds, name=name)
        identity = _account_identity(account)
        location = self._locations.get(identity) if identity else None

        if location is None:
            package_index = self.total_accounts // self.batch_size
            if package_index == len(self._packages):
                self._packages.append([])
            account_index = len(self._packages[package_index])
            self._packages[package_index].append(account)
            if identity:
                self._locations[identity] = (package_index, account_index)
            self.total_accounts += 1
        else:
            package_index, account_index = location
            self._packages[package_index][account_index] = account

        path = self._write_package(package_index)
        return {
            "path": path,
            "package_index": package_index + 1,
            "position": account_index + 1,
            "batch_size": self.batch_size,
            "total_accounts": self.total_accounts,
        }


def upsert_sub2api_import_bundle(auth_dir: Path, account: dict) -> Path:
    """把账号 upsert 进目录内合并导入包 sub2api_accounts_import.json。"""
    auth_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = auth_dir / SUB2API_IMPORT_BUNDLE_NAME
    accounts: list = []
    proxies: list = []
    if bundle_path.exists():
        try:
            existing = json.loads(bundle_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                if isinstance(existing.get("accounts"), list):
                    accounts = existing["accounts"]
                if isinstance(existing.get("proxies"), list):
                    proxies = existing["proxies"]
        except Exception:
            accounts = []
            proxies = []

    identity = _account_identity(account)
    kept = []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        if identity and _account_identity(item) == identity:
            continue
        kept.append(item)
    kept.append(account)
    payload = build_sub2api_data_payload(kept, proxies=proxies)
    tmp = bundle_path.with_suffix(bundle_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, bundle_path)
    return bundle_path


def write_sub2api_auth(auth_dir: Path, creds: dict) -> Path:
    """写出可直接用于 Sub2API「导入数据」的 JSON（单账号导出包），并更新合并包。"""
    auth_dir.mkdir(parents=True, exist_ok=True)
    account = build_sub2api_account_payload(creds)
    payload = build_sub2api_data_payload([account])
    path = auth_dir / sub2api_auth_filename(creds)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    upsert_sub2api_import_bundle(auth_dir, account)
    return path


def _is_jwt(token: str) -> bool:
    t = str(token or "").strip()
    return t.startswith("eyJ") and t.count(".") >= 2


def _sub2api_admin_headers(token: str) -> dict:
    """Sub2API admin 鉴权：
    - JWT (eyJxxx.yyy.zzz) → Authorization: Bearer <jwt>
    - 静态 admin API Key (admin-xxx / 任意非 JWT) → x-api-key: <key>
    参考：sub2api admin middleware 接受 x-api-key 头。
    """
    token = str(token or "").strip()
    headers = {"Content-Type": "application/json"}
    if not token:
        return headers
    if _is_jwt(token):
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers["x-api-key"] = token
    return headers


def upload_sub2api_account(
    base_url: str,
    admin_token: str,
    creds: dict,
    name: str = "",
    timeout: int = 30,
) -> dict:
    """通过 Sub2API 管理接口创建 Grok OAuth 账号。

    POST {base}/api/v1/admin/accounts
    鉴权：JWT → Authorization: Bearer；静态 admin API Key → x-api-key
    """
    import requests

    base = str(base_url or "").strip().rstrip("/")
    token = str(admin_token or "").strip()
    if not base:
        raise ValueError("sub2api_url 为空")
    if not token:
        raise ValueError("sub2api_token 为空")

    payload = build_sub2api_account_payload(creds, name=name)
    url = f"{base}/api/v1/admin/accounts"
    resp = requests.post(
        url,
        headers=_sub2api_admin_headers(token),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    )
    if resp.status_code >= 400:
        body = (resp.text or "").strip()
        if len(body) > 300:
            body = body[:300] + "..."
        raise RuntimeError(f"Sub2API 创建账号失败 HTTP {resp.status_code}: {body or resp.reason}")
    try:
        return resp.json()
    except Exception:
        return {"raw": (resp.text or "")[:300]}


def upload_cpa_auth_remote(
    base_url: str,
    management_key: str,
    record: dict,
    timeout: int = 30,
) -> str:
    """通过 CPA Management API 上传 auth 文件到远程实例。

    POST /v0/management/auth-files?name=<file.json>
    Header: Authorization: Bearer <management_key>
    Body: raw JSON auth record
    """
    import requests

    base = str(base_url or "").strip().rstrip("/")
    key = str(management_key or "").strip()
    if not base:
        raise ValueError("cpa_remote_url 为空")
    if not key:
        raise ValueError("cpa_management_key 为空")

    name = cpa_auth_filename(record)
    url = f"{base}/v0/management/auth-files"
    resp = requests.post(
        url,
        params={"name": name},
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(record, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    )
    if resp.status_code >= 400:
        body = (resp.text or "").strip()
        if len(body) > 300:
            body = body[:300] + "..."
        raise RuntimeError(f"远程上传失败 HTTP {resp.status_code}: {body or resp.reason}")
    return name


def write_auth_json(path: Path, auth_key: str, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {auth_key: entry}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def merge_auth_json(path: Path, auth_key: str, entry: dict, unique: bool = True) -> None:
    """
    合并写入。unique=True 时 key 变成 issuer::client_id::user_id，避免多账号互相覆盖。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    key = auth_key
    if unique and entry.get("user_id"):
        key = f"{auth_key}::{entry['user_id']}"
    existing[key] = entry
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_sso_list(path: str | None, single: str | None) -> list[str]:
    if single:
        return [single.strip()]
    if not path:
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 兼容 邮箱----密码----sso
        if "----" in line:
            parts = line.split("----")
            line = parts[-1].strip()
        out.append(line)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SSO cookie → grok auth.json (纯 HTTP)")
    ap.add_argument("--sso", metavar="FILE", help="sso 列表文件（一行一个 JWT，或 邮箱----密码----sso）")
    ap.add_argument("--sso-cookie", metavar="JWT", help="单个 sso cookie")
    ap.add_argument("--out", default=None, help="输出 auth.json 路径（单账号或 --merge）")
    ap.add_argument(
        "--out-dir",
        default=None,
        help="批量时每个账号写一个 {user_id}.json（可直接 cp 到 ~/.grok/auth.json）",
    )
    ap.add_argument(
        "--merge",
        action="store_true",
        help="合并到 --out，key 用 issuer::client_id::user_id",
    )
    ap.add_argument("--delay", type=int, default=0, help="每个间隔秒数")
    ap.add_argument("--email", default="", help="写入 entry.email（可选）")
    ap.add_argument(
        "--cpa-auth-dir",
        default=None,
        help="额外写出 CLIProxyAPI 扁平格式 xai-<email>.json 到该目录（CPA 热加载）",
    )
    ap.add_argument(
        "--cpa-remote-url",
        default=None,
        help="远程 CPA 地址，如 http://你的CPA地址:8317；配合 --cpa-management-key 通过 Management API 上传",
    )
    ap.add_argument(
        "--cpa-management-key",
        default=None,
        help="远程 CPA 管理密钥（remote-management.secret-key 明文）",
    )
    ap.add_argument(
        "--sub2api-dir",
        default=None,
        help="写出 Sub2API 导入包 JSON（type=sub2api-data，可直接「导入数据」）到该目录",
    )
    ap.add_argument(
        "--sub2api-url",
        default=None,
        help="Sub2API 地址，如 http://127.0.0.1:8080；配合 --sub2api-token 通过管理 API 创建账号",
    )
    ap.add_argument(
        "--sub2api-token",
        default=None,
        help="Sub2API 管理员 Bearer Token（Authorization）",
    )
    ap.add_argument("--proxy", default="", help="device-flow 走代理，如 http://127.0.0.1:7890")
    args = ap.parse_args()

    cookies = load_sso_list(args.sso, args.sso_cookie)
    if not cookies:
        ap.error("需要 --sso 或 --sso-cookie")

    if args.cpa_remote_url and not args.cpa_management_key:
        ap.error("使用 --cpa-remote-url 时必须同时提供 --cpa-management-key")
    if args.cpa_management_key and not args.cpa_remote_url:
        ap.error("使用 --cpa-management-key 时必须同时提供 --cpa-remote-url")
    if args.sub2api_url and not args.sub2api_token:
        ap.error("使用 --sub2api-url 时必须同时提供 --sub2api-token")
    if args.sub2api_token and not args.sub2api_url:
        ap.error("使用 --sub2api-token 时必须同时提供 --sub2api-url")

    export_only = bool(
        args.cpa_auth_dir or args.cpa_remote_url or args.sub2api_dir or args.sub2api_url
    ) and not args.out and not args.out_dir and not args.merge

    if len(cookies) > 1 and not args.out_dir and not args.merge and not export_only:
        # 默认批量写目录（仅在未指定 CPA/Sub2API 目标时）
        args.out_dir = "./auth_out"
        print(f"批量模式默认 --out-dir {args.out_dir}")

    # 只指定导出目标时不再默认写官方 ~/.grok/auth.json / uuid.json
    if (
        args.out is None
        and args.out_dir is None
        and not args.cpa_auth_dir
        and not args.cpa_remote_url
        and not args.sub2api_dir
        and not args.sub2api_url
        and len(cookies) == 1
    ):
        args.out = str(Path.home() / ".grok" / "auth.json")

    print(f"[*] SSO → auth.json: {len(cookies)} 个, delay={args.delay}s")
    ok = 0
    fail = 0

    for i, sso in enumerate(cookies, 1):
        print(f"\n{'=' * 60}\n[{i}/{len(cookies)}] ...\n{'=' * 60}")
        try:
            token = sso_to_token(sso, proxy=args.proxy)
            if not token:
                fail += 1
                print(f"  [x] [{i}] 失败")
                continue
            key, entry = token_to_auth_entry(token, email=args.email)
            uid = entry.get("user_id") or secrets.token_hex(4)

            if args.out_dir:
                p = Path(args.out_dir) / f"{uid}.json"
                write_auth_json(p, key, entry)
                print(f"  [save] {p}")
            if args.out:
                if args.merge or len(cookies) > 1:
                    merge_auth_json(Path(args.out), key, entry, unique=True)
                    print(f"  [save] merge → {args.out}")
                else:
                    write_auth_json(Path(args.out), key, entry)
                    print(f"  [save] {args.out}")

            if args.cpa_auth_dir or args.cpa_remote_url:
                record = token_to_cpa_record(token, email=args.email)
                if args.cpa_auth_dir:
                    cp = write_cpa_auth(Path(args.cpa_auth_dir), record)
                    print(f"  [save] CPA 本地 → {cp}")
                if args.cpa_remote_url:
                    name = upload_cpa_auth_remote(
                        args.cpa_remote_url,
                        args.cpa_management_key,
                        record,
                    )
                    print(f"  [save] CPA 远程 → {args.cpa_remote_url.rstrip('/')}/.../{name}")

            if args.sub2api_dir or args.sub2api_url:
                creds = token_to_sub2api_credentials(token, email=args.email)
                if args.sub2api_dir:
                    sp = write_sub2api_auth(Path(args.sub2api_dir), creds)
                    print(f"  [save] Sub2API 本地 → {sp}")
                if args.sub2api_url:
                    created = upload_sub2api_account(
                        args.sub2api_url,
                        args.sub2api_token,
                        creds,
                        name=args.email,
                    )
                    created_id = ""
                    if isinstance(created, dict):
                        data = created.get("data") if isinstance(created.get("data"), dict) else created
                        created_id = str((data or {}).get("id") or "")
                    print(f"  [save] Sub2API 远程账号已创建{(' id=' + created_id) if created_id else ''}")

            ok += 1
            print(f"  [ok] [{i}] 完成 user_id={uid[:12]}...")
        except Exception as e:
            fail += 1
            print(f"  [x] [{i}] 异常: {e}")

        if args.delay > 0 and i < len(cookies):
            time.sleep(args.delay)

    print(f"\n{'=' * 60}\n[=] 完成: {ok}/{len(cookies)} 成功, {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
