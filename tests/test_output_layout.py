import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import grok_register_ttk as app
import sso_to_auth_json as s2a


class ParseSsoLineTests(unittest.TestCase):
    def test_plain_sso(self):
        email, sso = s2a.parse_sso_line("eyJhbGciOiJIUzI1NiJ9.e30.sig")
        self.assertEqual(email, "")
        self.assertTrue(sso.startswith("eyJ"))

    def test_email_password_sso(self):
        email, sso = s2a.parse_sso_line(
            "a@b.com----pass----eyJhbGciOiJIUzI1NiJ9.e30.sig"
        )
        self.assertEqual(email, "a@b.com")
        self.assertEqual(sso, "eyJhbGciOiJIUzI1NiJ9.e30.sig")

    def test_failed_line_with_reason(self):
        email, sso = s2a.parse_sso_line(
            "a@b.com----pass----eyJhbGciOiJIUzI1NiJ9.e30.sig----换 token 或验活失败"
        )
        self.assertEqual(email, "a@b.com")
        self.assertEqual(sso, "eyJhbGciOiJIUzI1NiJ9.e30.sig")


class OutputLayoutTests(unittest.TestCase):
    def test_sso_and_verified_token_are_separated(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, "APP_DIR", tmp), patch.object(
                app, "OUTPUT_ROOT", os.path.join(tmp, "output")
            ), patch.object(
                app, "RUNS_ROOT", os.path.join(tmp, "output", "runs")
            ), patch.object(
                app, "FAILED_SSO_ROOT", os.path.join(tmp, "output", "failed_sso")
            ), patch.object(
                app, "LEGACY_OUTPUT_ROOT", os.path.join(tmp, "output", "legacy")
            ), patch.object(
                app, "MAIL_OUTPUT_ROOT", os.path.join(tmp, "output", "mail")
            ), patch.object(
                app, "CPA_OUTPUT_ROOT", os.path.join(tmp, "output", "cpa")
            ), patch.object(
                app, "SUB2API_OUTPUT_ROOT", os.path.join(tmp, "output", "sub2api")
            ):
                info = app.begin_run_output(stamp="20260720_210000")
                app.persist_obtained_sso(
                    "raw@ex.com",
                    "pwd",
                    "eyJhbGciOiJIUzI1NiJ9.e30.sig",
                    accounts_file=info["accounts_file"],
                )
                app.persist_verified_token(
                    "ok@ex.com",
                    "pwd2",
                    "eyJhbGciOiJIUzI1NiJ9.e30.sig",
                    {
                        "access_token": "access-ok",
                        "refresh_token": "refresh-ok",
                        "expires_at": "2099-01-01T00:00:00Z",
                        "base_url": "https://cli-chat-proxy.grok.com/v1",
                    },
                )
                app.persist_failed_sso(
                    "bad@ex.com",
                    "pwd3",
                    "eyJhbGciOiJIUzI1NiJ9.e31.sig",
                    reason="验活失败",
                )
                app.persist_hold_402(
                    "hold@ex.com",
                    "pwd4",
                    "eyJhbGciOiJIUzI1NiJ9.e32.sig",
                    {
                        "access_token": "access-hold",
                        "refresh_token": "refresh-hold",
                        "expires_at": "2099-01-01T00:00:00Z",
                        "base_url": "https://cli-chat-proxy.grok.com/v1",
                    },
                    reason="HTTP 402 spending-limit",
                )
                app.persist_discard_403(
                    "drop@ex.com",
                    "pwd5",
                    "eyJhbGciOiJIUzI1NiJ9.e33.sig",
                    reason="HTTP 403 permission-denied",
                )

                accounts = Path(info["accounts_file"]).read_text(encoding="utf-8")
                verified = Path(info["verified_accounts_file"]).read_text(encoding="utf-8")
                verified_jsonl = Path(info["verified_file"]).read_text(encoding="utf-8")
                failed = Path(info["failed_file"]).read_text(encoding="utf-8")
                hold = Path(info["hold_402_file"]).read_text(encoding="utf-8")
                discard = Path(info["discard_403_file"]).read_text(encoding="utf-8")

                self.assertIn("sso", Path(info["accounts_file"]).parts)
                self.assertIn("verified", Path(info["verified_file"]).parts)
                self.assertIn("hold_402", Path(info["hold_402_file"]).parts)
                self.assertIn("discard_403", Path(info["discard_403_file"]).parts)
                self.assertIn("raw@ex.com----pwd----eyJ", accounts)
                self.assertIn("ok@ex.com----pwd2----access-ok", verified)
                self.assertNotIn("eyJhbGciOiJIUzI1NiJ9.e30.sig", verified)
                self.assertIn("access-ok", verified_jsonl)
                self.assertIn("bad@ex.com----pwd3----eyJ", failed)
                self.assertIn("access-hold", hold)
                self.assertIn("drop@ex.com", discard)


class Sub2APISuccessGateTests(unittest.TestCase):
    def setUp(self):
        self.original_config = app.config.copy()

    def tearDown(self):
        app.wait_sub2api_pending()
        app.config = self.original_config

    def test_remote_upload_failure_after_verify_still_counts_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app.config.update(
                {
                    "sub2api_auto_add": True,
                    "sub2api_dir": temp_dir,
                    "sub2api_url": "https://sub2api.example.invalid",
                    "sub2api_token": "bad-token",
                    "sub2api_batch_size": 20,
                    "sub2api_verify": True,
                    "sub2api_verify_workers": 1,
                }
            )
            token = {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
            with patch.object(app, "OUTPUT_ROOT", os.path.join(temp_dir, "output")), patch.object(
                app, "RUNS_ROOT", os.path.join(temp_dir, "output", "runs")
            ), patch.object(
                app, "FAILED_SSO_ROOT", os.path.join(temp_dir, "output", "failed_sso")
            ), patch.object(
                app, "LEGACY_OUTPUT_ROOT", os.path.join(temp_dir, "output", "legacy")
            ), patch.object(
                app, "MAIL_OUTPUT_ROOT", os.path.join(temp_dir, "output", "mail")
            ), patch.object(
                app, "CPA_OUTPUT_ROOT", os.path.join(temp_dir, "output", "cpa")
            ), patch.object(
                app, "SUB2API_OUTPUT_ROOT", os.path.join(temp_dir, "output", "sub2api")
            ):
                app.begin_run_output(stamp="20260720_testgate")
                app.begin_sub2api_batch_session()
                with patch.object(app._s2cpa, "sso_to_token", return_value=token), patch.object(
                    app._s2cpa,
                    "verify_grok_chat",
                    return_value=(app._s2cpa.VERDICT_ALIVE, "HTTP 200"),
                ), patch.object(
                    app._s2cpa,
                    "upload_sub2api_account",
                    side_effect=RuntimeError("Sub2API 创建账号失败 HTTP 401"),
                ):
                    ok = app.wait_sub2api_account_result(
                        app.add_sso_to_sub2api(
                            "sso-cookie",
                            email="ok@example.com",
                            password="pwd",
                        )
                    )
                    app.wait_sub2api_pending()
                self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
