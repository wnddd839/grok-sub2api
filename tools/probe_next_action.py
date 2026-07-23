#!/usr/bin/env python3
"""Probe consent Next-Action IDs (local SSO from output/, no token in stdout)."""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sso_to_auth_json as mod  # noqa: E402
from curl_cffi import requests  # noqa: E402


def main() -> int:
    entries, files = mod.scan_sso_entries(ROOT)
    if not entries:
        print("SSO_FOUND=no")
        return 2

    run_dirs = sorted((ROOT / "output" / "runs").glob("*"), reverse=True)
    email, sso = entries[0]
    src = str(files[0]) if files else "scan"
    for rd in run_dirs:
        p = rd / "sso" / "accounts.txt"
        if not p.is_file():
            continue
        first = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if not first:
            continue
        e, s = mod.parse_sso_line(first[0])
        if s:
            email, sso = e, s
            src = str(p)
            break

    domain = email.split("@")[-1].lower() if "@" in email else "(no-email)"
    print("SSO_FOUND=yes")
    print(f"SSO_SOURCE={src}")
    print(f"EMAIL_DOMAIN={domain}")

    proxy = ""
    cfg = ROOT / "config.json"
    if cfg.is_file():
        proxy = (json.loads(cfg.read_text(encoding="utf-8")).get("proxy") or "").strip()
    print(f"PROXY={proxy or 'none'}")

    proxies = {"http": proxy, "https": proxy} if proxy else None
    s = requests.Session()
    if proxies:
        s.proxies = proxies
    for cookie_domain in (".x.ai", "accounts.x.ai", "auth.x.ai"):
        s.cookies.set("sso", sso, domain=cookie_domain)
        s.cookies.set("sso-rw", sso, domain=cookie_domain)

    try:
        r = s.get("https://accounts.x.ai/", impersonate="chrome", timeout=20)
    except Exception as e:
        print(f"ACCOUNTS_CHECK_ERROR={e}")
        return 1
    if "sign-in" in r.url or "sign-up" in r.url:
        print("SSO_VALID=no")
        return 3
    print("SSO_VALID=yes")

    _verifier, challenge, state, nonce = mod._gen_pkce()
    authorize_params = urllib.parse.urlencode(
        {
            "client_id": mod.CLIENT_ID,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
            "plan": mod.GROK_PLAN,
            "redirect_uri": mod.REDIRECT_URI,
            "referrer": mod.GROK_REFERRER,
            "response_type": "code",
            "scope": mod.SCOPES,
            "state": state,
        }
    )
    authorize_url = f"{mod.OIDC_ISSUER}/oauth2/authorize?{authorize_params}"

    try:
        resp = s.get(authorize_url, impersonate="chrome", timeout=20, allow_redirects=True)
    except Exception as e:
        print(f"AUTHORIZE_ERROR={e}")
        return 4

    final_url = str(resp.url)
    html = str(resp.text or "")
    if len(final_url) > 120:
        print(f"CONSENT_URL={final_url[:120]}...")
    else:
        print(f"CONSENT_URL={final_url}")

    if "/oauth2/consent" not in final_url:
        print(f"CONSENT_REACHED=no final={final_url}")
        return 5

    def log(msg: str) -> None:
        print(msg, flush=True)

    action_ids = mod._discover_action_ids_from_js(
        s, html, base_url="https://accounts.x.ai", log=log
    )
    for aid in [mod.NEXT_ACTION_ID.lower()] + mod._extract_next_action_ids(html):
        if aid not in action_ids:
            action_ids.insert(0, aid) if aid == mod.NEXT_ACTION_ID.lower() else action_ids.append(aid)

    print(f"CANDIDATES_COUNT={len(action_ids)}")
    print("CANDIDATES_ORDER=" + ",".join(a[:12] + "..." for a in action_ids[:15]))

    consent_payload = json.dumps(
        [
            {
                "action": "allow",
                "clientId": mod.CLIENT_ID,
                "redirectUri": mod.REDIRECT_URI,
                "scope": mod.SCOPES,
                "state": state,
                "codeChallenge": challenge,
                "codeChallengeMethod": "S256",
                "nonce": nonce,
                "principalType": "User",
                "principalId": "",
                "referrer": mod.GROK_REFERRER,
            }
        ]
    )

    results: list[tuple[str, str, str]] = []
    working: str | None = None
    max_try = min(12, len(action_ids))
    for action_id in action_ids[:max_try]:
        try:
            r = s.post(
                final_url,
                data=consent_payload,
                headers={
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Accept": "text/x-component",
                    "Origin": "https://accounts.x.ai",
                    "Referer": final_url,
                    "Next-Action": action_id,
                },
                impersonate="chrome",
                timeout=20,
                allow_redirects=True,
            )
        except Exception as e:
            results.append((action_id, "EXCEPTION", str(e)[:120]))
            continue
        body = str(r.text or "")
        bl = body.lower()
        if r.status_code == 404 or "server action not found" in bl:
            results.append((action_id, "404", body[:100].replace("\n", " ")))
            continue
        code = mod._parse_consent_code(body)
        if code:
            working = action_id
            results.append((action_id, "SUCCESS", f"code_len={len(code)}"))
            break
        results.append(
            (action_id, f"HTTP_{r.status_code}_NO_CODE", body[:120].replace("\n", " "))
        )

    print("---TRIES---")
    for aid, status, detail in results:
        print(f"{aid}\t{status}\t{detail}")

    non404 = [aid for aid, st, _ in results if st != "404"]
    print(f"NON404_COUNT={len(non404)}")
    if working:
        print(f"BEST_NEXT_ACTION_ID={working}")
    else:
        print("BEST_NEXT_ACTION_ID=")
        if non404:
            print(f"BEST_NON404_CANDIDATE={non404[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
