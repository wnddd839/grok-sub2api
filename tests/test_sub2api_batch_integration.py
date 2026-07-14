import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import grok_register_ttk as app


class Sub2APIBatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.original_config = app.config.copy()
        self.original_writer = getattr(app, "_sub2api_batch_writer", None)

    def tearDown(self):
        app.wait_sub2api_pending()
        app.config = self.original_config
        app._sub2api_batch_writer = self.original_writer

    def test_registration_session_writes_to_timestamped_twenty_account_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app.config.update(
                {
                    "sub2api_auto_add": True,
                    "sub2api_dir": temp_dir,
                    "sub2api_url": "",
                    "sub2api_token": "",
                    "sub2api_batch_size": 20,
                    "sub2api_verify": True,
                    "sub2api_verify_workers": 2,
                }
            )
            writer = app.begin_sub2api_batch_session()
            token = {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "token_type": "Bearer",
                "expires_in": 3600,
            }

            with patch.object(app._s2cpa, "sso_to_token", return_value=token), patch.object(
                app._s2cpa, "verify_grok_credentials", return_value=(True, "HTTP 200")
            ):
                app.add_sso_to_sub2api("sso-cookie", email="first@example.com")
                app.wait_sub2api_pending()

            package_paths = list(
                writer.session_dir.glob("sub2api_accounts_*.json")
            )
            payload = json.loads(package_paths[0].read_text(encoding="utf-8"))

            self.assertEqual(writer.batch_size, 20)
            self.assertEqual(writer.total_accounts, 1)
            self.assertEqual(len(package_paths), 1)
            self.assertEqual(len(payload["accounts"]), 1)
            self.assertEqual(
                payload["accounts"][0]["credentials"]["email"],
                "first@example.com",
            )
            self.assertEqual(writer.session_dir.parent, Path(temp_dir))

    def test_failed_verify_skips_local_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app.config.update(
                {
                    "sub2api_auto_add": True,
                    "sub2api_dir": temp_dir,
                    "sub2api_url": "",
                    "sub2api_token": "",
                    "sub2api_batch_size": 20,
                    "sub2api_verify": True,
                    "sub2api_verify_workers": 2,
                }
            )
            writer = app.begin_sub2api_batch_session()
            token = {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "token_type": "Bearer",
                "expires_in": 3600,
            }

            with patch.object(app._s2cpa, "sso_to_token", return_value=token), patch.object(
                app._s2cpa,
                "verify_grok_credentials",
                return_value=(False, "HTTP 401"),
            ):
                app.add_sso_to_sub2api("sso-cookie", email="dead@example.com")
                app.wait_sub2api_pending()

            self.assertEqual(writer.total_accounts, 0)
            self.assertEqual(list(writer.session_dir.glob("sub2api_accounts_*.json")), [])


if __name__ == "__main__":
    unittest.main()
