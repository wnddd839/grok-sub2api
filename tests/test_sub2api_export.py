import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sso_to_auth_json as converter


def sample_credentials(email: str = "test@example.com") -> dict:
    return {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "id_token": "id-token",
        "token_type": "Bearer",
        "expires_at": "2026-07-13T07:56:53Z",
        "email": email,
        "client_id": converter.SUB2API_CLIENT_ID,
        "scope": converter.SUB2API_SCOPE,
        "base_url": converter.SUB2API_BASE_URL,
    }


class Sub2APIExportTests(unittest.TestCase):
    def test_data_payload_matches_sub2api_import_header(self):
        account = converter.build_sub2api_account_payload(sample_credentials())

        payload = converter.build_sub2api_data_payload([account])

        self.assertEqual(payload["type"], "sub2api-data")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["proxies"], [])
        self.assertEqual(payload["accounts"], [account])

    def test_write_auth_creates_importable_single_and_merged_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            first_path = converter.write_sub2api_auth(
                output_dir, sample_credentials("first@example.com")
            )
            converter.write_sub2api_auth(
                output_dir, sample_credentials("second@example.com")
            )
            converter.write_sub2api_auth(
                output_dir, sample_credentials("first@example.com")
            )

            single = json.loads(first_path.read_text(encoding="utf-8"))
            merged = json.loads(
                (output_dir / converter.SUB2API_IMPORT_BUNDLE_NAME).read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(single["type"], "sub2api-data")
            self.assertEqual(len(single["accounts"]), 1)
            self.assertEqual(len(merged["accounts"]), 2)
            self.assertEqual(
                {item["credentials"]["email"] for item in merged["accounts"]},
                {"first@example.com", "second@example.com"},
            )

    def test_account_payload_has_unix_expiry_for_import(self):
        account = converter.build_sub2api_account_payload(sample_credentials())

        self.assertEqual(account["platform"], "grok")
        self.assertEqual(account["type"], "oauth")
        self.assertIsInstance(account["expires_at"], int)
        self.assertGreater(account["expires_at"], 0)
        # Grok OAuth 靠 refresh_token 续命；开启后 access 到期会被踢出调度，永远刷不到
        self.assertFalse(account["auto_pause_on_expired"])

    def test_batch_writer_splits_twenty_one_accounts_into_twenty_and_one(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = converter.Sub2APIBatchWriter(
                Path(temp_dir), batch_size=20, session_name="batch_test"
            )

            for index in range(21):
                writer.add_credentials(
                    sample_credentials(f"account-{index:02d}@example.com")
                )

            package_paths = sorted(writer.session_dir.glob("sub2api_accounts_*.json"))
            package_sizes = [
                len(json.loads(path.read_text(encoding="utf-8"))["accounts"])
                for path in package_paths
            ]

            self.assertEqual(package_sizes, [20, 1])
            self.assertEqual(writer.total_accounts, 21)
            self.assertEqual(list(writer.session_dir.glob("grok-*.json")), [])

    def test_batch_writer_updates_duplicate_without_advancing_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = converter.Sub2APIBatchWriter(
                Path(temp_dir), batch_size=20, session_name="batch_test"
            )
            first = sample_credentials("same@example.com")
            updated = sample_credentials("same@example.com")
            updated["access_token"] = "updated-access-token"

            writer.add_credentials(first)
            result = writer.add_credentials(updated)

            package = json.loads(result["path"].read_text(encoding="utf-8"))
            self.assertEqual(writer.total_accounts, 1)
            self.assertEqual(len(package["accounts"]), 1)
            self.assertEqual(
                package["accounts"][0]["credentials"]["access_token"],
                "updated-access-token",
            )
            self.assertEqual(result["position"], 1)

    def test_verify_grok_credentials_accepts_responses_200(self):
        response = MagicMock()
        response.status_code = 200
        response.text = '{"id":"1"}'
        with patch.object(converter.requests, "post", return_value=response) as mocked:
            ok, message = converter.verify_grok_credentials(sample_credentials())

        self.assertTrue(ok)
        self.assertIn("200", message)
        self.assertIn("/responses", mocked.call_args.args[0])
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["headers"]["x-grok-client-version"], "0.2.93")
        self.assertEqual(kwargs["headers"]["X-XAI-Token-Auth"], "xai-grok-cli")

    def test_verify_grok_chat_classifies_402_and_403(self):
        self.assertEqual(
            converter.classify_grok_chat_status(
                402, '{"code":"personal-team-blocked:spending-limit"}'
            ),
            converter.VERDICT_HOLD_402,
        )
        self.assertEqual(
            converter.classify_grok_chat_status(
                403, '{"code":"permission-denied","error":"Access to the chat endpoint is denied."}'
            ),
            converter.VERDICT_DROP_403,
        )

    def test_verify_grok_credentials_rejects_401(self):
        response = MagicMock()
        response.status_code = 401
        response.text = "unauthorized"
        with patch.object(converter.requests, "post", return_value=response):
            ok, message = converter.verify_grok_credentials(sample_credentials())

        self.assertFalse(ok)
        self.assertIn("401", message)


if __name__ == "__main__":
    unittest.main()
