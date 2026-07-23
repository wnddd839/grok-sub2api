import json
import tempfile
import unittest
from pathlib import Path

import sso_to_auth_json as converter


def _fake_jwt(payload: dict) -> str:
    import base64

    def b64(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64(payload)}."


class Sub2apiToCpaTests(unittest.TestCase):
    def test_bundle_converts_to_flat_cpa_files(self):
        access = _fake_jwt(
            {
                "sub": "user-1",
                "email": "a@example.com",
                "exp": 2000000000,
                "referrer": "grok-build",
            }
        )
        creds = {
            "access_token": access,
            "refresh_token": "refresh-a",
            "id_token": "id-a",
            "token_type": "Bearer",
            "expires_at": "2033-05-18T03:33:20Z",
            "email": "a@example.com",
            "client_id": converter.SUB2API_CLIENT_ID,
            "scope": converter.SUB2API_SCOPE,
            "base_url": converter.SUB2API_BASE_URL,
        }
        account = converter.build_sub2api_account_payload(creds)
        bundle = converter.build_sub2api_data_payload([account])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            src = root / "sub2api_accounts_001.json"
            src.write_text(json.dumps(bundle), encoding="utf-8")
            out = root / "cpa"

            stats = converter.convert_sub2api_path_to_cpa(src, out)

            self.assertEqual(stats["ok"], 1)
            self.assertEqual(stats["fail"], 0)
            path = out / "xai-a@example.com.json"
            self.assertTrue(path.exists())
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["type"], "xai")
            self.assertEqual(record["auth_kind"], "oauth")
            self.assertEqual(record["email"], "a@example.com")
            self.assertEqual(record["access_token"], access)
            self.assertEqual(record["refresh_token"], "refresh-a")
            self.assertEqual(record["base_url"], converter.CPA_GROK_BASE_URL)
            self.assertIn("headers", record)

    def test_directory_dedupes_same_email(self):
        access = _fake_jwt({"sub": "u", "email": "dup@example.com", "exp": 2000000000})
        creds = {
            "access_token": access,
            "refresh_token": "r",
            "email": "dup@example.com",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("a.json", "b.json"):
                payload = converter.build_sub2api_data_payload(
                    [converter.build_sub2api_account_payload(creds)]
                )
                (root / name).write_text(json.dumps(payload), encoding="utf-8")

            stats = converter.convert_sub2api_path_to_cpa(root, root / "cpa")
            self.assertEqual(stats["ok"], 1)
            self.assertEqual(stats["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
