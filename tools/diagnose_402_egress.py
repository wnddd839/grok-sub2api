#!/usr/bin/env python3
"""对照诊断：402 spending-limit 是「本机先打脏」还是「Sub2API 服务器出口被锁」。

需要两个「从未打过 /responses」的新号 SSO（各跑一条）。

案例 A — local-first（本机先发第一次真实请求）
  SSO → 本机换 token → 本机直连 cli-chat-proxy /responses
  不推 Sub2API。

案例 B — server-first（本机绝不碰 /models、/responses）
  SSO → 本机换 token → 远程创建到 Sub2API → 用网关 API Key 发第一次请求
  （请求由服务器出口打到 Grok）

判定：
  A=402 且 B=200 → 本机出口有问题，关本地验活 / 换代理注册；服务器可先不配代理
  A=200 且 B=402 → 服务器 IP 被锁，去 Sub2API 绑住宅代理
  A=B=402       → 更像账号额度/风控（与出口无关），或两边出口都脏
  A=B=200       → 不是固定出口问题，再查模型名 / 调度 / 旧号污染

用法：
  # 案例 A
  python tools/diagnose_402_egress.py local-first --sso "eyJ..." --email a@x.com

  # 案例 B（需 Sub2API 用户 API Key，不是 admin JWT）
  python tools/diagnose_402_egress.py server-first --sso "eyJ..." --email b@x.com ^
      --api-key "sk-..."

  # 从 accounts 行解析：email----password----sso
  python tools/diagnose_402_egress.py local-first --line "a@x.com----pass----eyJ..."

配置默认读项目根 config.json 的 proxy / sub2api_url / sub2api_token。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sso_to_auth_json as s2  # noqa: E402


def load_config(path: Path | None = None) -> dict:
    cfg_path = path or (ROOT / "config.json")
    if not cfg_path.is_file():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def parse_account_line(line: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in str(line or "").strip().split("----")]
    if len(parts) >= 3 and parts[2]:
        return parts[0], parts[1], parts[2]
    if len(parts) == 1 and parts[0]:
        return "", "", parts[0]
    raise ValueError("无法解析账号行，期望 email----password----sso 或纯 sso")


def resolve_sso(args: argparse.Namespace) -> tuple[str, str]:
    email = str(getattr(args, "email", "") or "").strip()
    sso = str(getattr(args, "sso", "") or "").strip()
    line = str(getattr(args, "line", "") or "").strip()
    if line:
        e, _pw, s = parse_account_line(line)
        email = email or e
        sso = sso or s
    if not sso:
        raise SystemExit("请提供 --sso 或 --line")
    return email, sso


def lookup_egress_ip(proxy: str = "", timeout: int = 10) -> str:
    """查本机（或代理）出口 IP，方便和服务器出口对照。"""
    from curl_cffi import requests

    kwargs: dict[str, Any] = {"timeout": timeout, "impersonate": "chrome"}
    proxy = str(proxy or "").strip()
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    for url in (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
    ):
        try:
            resp = requests.get(url, **kwargs)
            text = (resp.text or "").strip()
            if resp.status_code == 200 and text and len(text) < 80:
                return text
        except Exception:
            continue
    return "(unknown)"


def exchange_token(sso: str, email: str, proxy: str) -> dict:
    def _log(msg: str) -> None:
        print(f"  [token] {msg}")

    token = s2.sso_to_token(sso, proxy=proxy, log=_log)
    if not token:
        raise RuntimeError("换 token 失败")
    return s2.token_to_sub2api_credentials(token, email=email)


def classify_status(status: int | None, body: str) -> str:
    text = (body or "").lower()
    if status is None:
        return "NETWORK_ERROR"
    if status == 402 or "spending-limit" in text or "personal-team-blocked" in text:
        return "SPENDING_LIMIT_402"
    if 200 <= status < 300:
        return "OK"
    if status in (401, 403):
        return "AUTH_FAIL"
    return f"HTTP_{status}"


def run_local_first(email: str, sso: str, proxy: str, model: str) -> dict:
    print("=== 案例 A: local-first ===")
    print("流程: SSO → 本机换 token → 本机 POST /responses（不推 Sub2API）")
    egress = lookup_egress_ip(proxy=proxy)
    print(f"本机出口 IP: {egress}")
    print("换 token ...")
    creds = exchange_token(sso, email, proxy)
    print(f"email={creds.get('email') or email or '(none)'}")
    print(f"直连 {s2.CPA_PROBE_URL} model={model} ...")
    status, body = s2.probe_grok_responses(creds, proxy=proxy, model=model)
    verdict = classify_status(status, body)
    print(f"结果: status={status} verdict={verdict}")
    print(f"摘要: {body}")
    return {
        "case": "local-first",
        "egress_ip": egress,
        "status": status,
        "verdict": verdict,
        "body": body,
        "email": creds.get("email") or email,
    }


def call_sub2api_gateway(
    base_url: str,
    api_key: str,
    model: str,
    timeout: int = 60,
) -> tuple[int | None, str, str]:
    """经 Sub2API 网关发第一次真实请求。优先 /v1/responses，失败再试 chat/completions。"""
    import requests

    base = str(base_url or "").strip().rstrip("/")
    key = str(api_key or "").strip()
    if not base:
        raise ValueError("sub2api_url 为空")
    if not key:
        raise ValueError("sub2api_api_key 为空（网关 Key，不是 admin JWT）")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    attempts = [
        (
            "responses",
            f"{base}/v1/responses",
            {
                "model": model,
                "input": "ping",
                "max_output_tokens": 2,
                "stream": False,
            },
        ),
        (
            "chat",
            f"{base}/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 2,
                "stream": False,
            },
        ),
    ]
    last_status: int | None = None
    last_body = ""
    last_path = ""
    for path_name, url, payload in attempts:
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except Exception as exc:
            last_status = None
            last_body = str(exc)[:300]
            last_path = path_name
            continue
        body = (resp.text or "").replace("\n", " ").strip()
        last_status = int(resp.status_code)
        last_body = body[:300]
        last_path = path_name
        # 404/405 换下一个入口；其它状态直接返回
        if last_status in (404, 405):
            continue
        return last_status, last_body, last_path
    return last_status, last_body, last_path


def run_server_first(
    email: str,
    sso: str,
    proxy: str,
    sub2api_url: str,
    admin_token: str,
    api_key: str,
    model: str,
) -> dict:
    print("=== 案例 B: server-first ===")
    print("流程: SSO → 本机换 token（不打 /models、/responses）→ 推 Sub2API → 网关首次请求")
    local_ip = lookup_egress_ip(proxy=proxy)
    print(f"本机出口 IP（仅换 token，不应打 Grok /responses）: {local_ip}")
    print("换 token ...")
    creds = exchange_token(sso, email, proxy)
    label = creds.get("email") or email or "diagnose-server-first"
    print(f"email={label}")
    print(f"上传到 Sub2API {sub2api_url} ...")
    created = s2.upload_sub2api_account(
        sub2api_url, admin_token, creds, name=str(label)
    )
    account_id = ""
    if isinstance(created, dict):
        account_id = str(
            created.get("id")
            or (created.get("data") or {}).get("id")
            or ""
        )
    print(f"远程创建完成 id={account_id or '(see response)'}")
    if not api_key:
        print("未提供 --api-key / sub2api_api_key：账号已推上，但无法代发网关请求。")
        print("请立刻用该 Key 在 Sub2API 发一条最短请求，看是否 402。")
        return {
            "case": "server-first",
            "egress_ip": "server(unknown-until-request)",
            "status": None,
            "verdict": "UPLOADED_WAIT_GATEWAY",
            "body": "missing api_key",
            "email": label,
            "account_id": account_id,
        }
    print(f"经 Sub2API 网关发第一次请求 model={model} ...")
    status, body, path = call_sub2api_gateway(sub2api_url, api_key, model=model)
    verdict = classify_status(status, body)
    print(f"网关入口: {path}")
    print(f"结果: status={status} verdict={verdict}")
    print(f"摘要: {body}")
    return {
        "case": "server-first",
        "egress_ip": "server",
        "gateway_path": path,
        "status": status,
        "verdict": verdict,
        "body": body,
        "email": label,
        "account_id": account_id,
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="402 spending-limit 出口对照诊断")
    ap.add_argument(
        "case",
        choices=("local-first", "server-first", "A", "B"),
        help="A/local-first 或 B/server-first",
    )
    ap.add_argument("--sso", default="", help="纯 SSO cookie/JWT")
    ap.add_argument("--line", default="", help="email----password----sso")
    ap.add_argument("--email", default="", help="可选邮箱标签")
    ap.add_argument("--proxy", default="", help="覆盖 config.proxy")
    ap.add_argument("--model", default=s2.CPA_PROBE_MODEL, help="探测模型名")
    ap.add_argument("--sub2api-url", default="", help="覆盖 config.sub2api_url")
    ap.add_argument("--admin-token", default="", help="覆盖 config.sub2api_token")
    ap.add_argument(
        "--api-key",
        default="",
        help="Sub2API 用户 API Key（网关调用；不是 admin JWT）",
    )
    ap.add_argument("--config", default="", help="config.json 路径")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(Path(args.config) if args.config else None)
    proxy = str(args.proxy or cfg.get("proxy") or "").strip()
    email, sso = resolve_sso(args)
    case = args.case
    if case == "A":
        case = "local-first"
    elif case == "B":
        case = "server-first"

    if case == "local-first":
        result = run_local_first(email, sso, proxy, model=args.model)
    else:
        sub2_url = str(args.sub2api_url or cfg.get("sub2api_url") or "").strip()
        admin = str(args.admin_token or cfg.get("sub2api_token") or "").strip()
        api_key = str(
            args.api_key or cfg.get("sub2api_api_key") or ""
        ).strip()
        if not sub2_url or not admin:
            raise SystemExit("server-first 需要 sub2api_url + sub2api_token（admin）")
        result = run_server_first(
            email,
            sso,
            proxy,
            sub2_url,
            admin,
            api_key,
            model=args.model,
        )

    print("\n--- JSON ---")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        "\n对照提示: 用两个新号分别跑 A 和 B；"
        "A=402&B=200→本机问题；A=200&B=402→给 Sub2API 配代理；两边都 402→账号风控。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
