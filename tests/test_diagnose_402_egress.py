"""402 出口对照诊断：两个案例的单元测试（不打真实网络）。"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tools import diagnose_402_egress as diag


class Diagnose402EgressTests(unittest.TestCase):
    def test_classify_spending_limit(self):
        body = (
            '{"code":"personal-team-blocked:spending-limit",'
            '"error":"You have run out of credits"}'
        )
        self.assertEqual(diag.classify_status(402, body), "SPENDING_LIMIT_402")
        self.assertEqual(diag.classify_status(200, "ok"), "OK")

    def test_parse_account_line(self):
        email, pw, sso = diag.parse_account_line("a@b.com----pw----eyJabc")
        self.assertEqual(email, "a@b.com")
        self.assertEqual(pw, "pw")
        self.assertEqual(sso, "eyJabc")

    @patch.object(diag, "lookup_egress_ip", return_value="1.2.3.4")
    @patch.object(diag.s2, "probe_grok_responses", return_value=(402, "spending-limit"))
    @patch.object(diag, "exchange_token")
    def test_case_a_local_first_reports_402(
        self, mocked_exchange, _mocked_probe, _mocked_ip
    ):
        mocked_exchange.return_value = {
            "access_token": "tok",
            "email": "a@x.com",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        }
        result = diag.run_local_first("a@x.com", "sso", proxy="", model="grok-4.5")
        self.assertEqual(result["case"], "local-first")
        self.assertEqual(result["verdict"], "SPENDING_LIMIT_402")
        self.assertEqual(result["egress_ip"], "1.2.3.4")
        self.assertEqual(result["status"], 402)

    @patch.object(diag, "call_sub2api_gateway", return_value=(200, '{"id":"1"}', "responses"))
    @patch.object(diag.s2, "upload_sub2api_account", return_value={"id": "acc-1"})
    @patch.object(diag, "lookup_egress_ip", return_value="1.2.3.4")
    @patch.object(diag, "exchange_token")
    def test_case_b_server_first_ok_via_gateway(
        self, mocked_exchange, _ip, mocked_upload, mocked_gateway
    ):
        mocked_exchange.return_value = {
            "access_token": "tok",
            "email": "b@x.com",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        }
        result = diag.run_server_first(
            "b@x.com",
            "sso",
            proxy="",
            sub2api_url="https://sub2api.example",
            admin_token="admin",
            api_key="sk-test",
            model="grok-4.5",
        )
        self.assertEqual(result["case"], "server-first")
        self.assertEqual(result["verdict"], "OK")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["account_id"], "acc-1")
        mocked_upload.assert_called_once()
        mocked_gateway.assert_called_once()

    @patch.object(diag, "call_sub2api_gateway")
    @patch.object(diag.s2, "upload_sub2api_account", return_value={"id": "acc-2"})
    @patch.object(diag, "lookup_egress_ip", return_value="1.2.3.4")
    @patch.object(diag, "exchange_token")
    def test_case_b_without_api_key_stops_after_upload(
        self, mocked_exchange, _ip, mocked_upload, mocked_gateway
    ):
        mocked_exchange.return_value = {
            "access_token": "tok",
            "email": "c@x.com",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        }
        result = diag.run_server_first(
            "c@x.com",
            "sso",
            proxy="",
            sub2api_url="https://sub2api.example",
            admin_token="admin",
            api_key="",
            model="grok-4.5",
        )
        self.assertEqual(result["verdict"], "UPLOADED_WAIT_GATEWAY")
        mocked_upload.assert_called_once()
        mocked_gateway.assert_not_called()

    @patch("requests.post")
    def test_gateway_falls_back_from_responses_to_chat(self, mocked_post):
        resp_404 = MagicMock(status_code=404, text="no")
        resp_200 = MagicMock(status_code=200, text='{"ok":true}')
        mocked_post.side_effect = [resp_404, resp_200]
        status, body, path = diag.call_sub2api_gateway(
            "https://sub2api.example",
            "sk-test",
            model="grok-4.5",
        )
        self.assertEqual(status, 200)
        self.assertEqual(path, "chat")
        self.assertIn("ok", body)
        self.assertEqual(mocked_post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
