import unittest
from unittest.mock import patch

import grok_register_ttk as app


class DummyResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class CloudMailApiTests(unittest.TestCase):
    def setUp(self):
        self.original_config = app.config.copy()
        self.original_cf_domain_index = app._cf_domain_index
        app._cf_domain_index = 0
        app.config = app.DEFAULT_CONFIG.copy()
        app.config.update(
            {
                "email_provider": "cloud-mail",
                "cloud_mail_api_base": "https://mail.example.com",
                "cloud_mail_token": "test-token-uuid",
                "defaultDomains": "mail.example.com",
            }
        )

    def tearDown(self):
        app.config = self.original_config
        app._cf_domain_index = self.original_cf_domain_index

    def test_create_address_uses_adduser_payload(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse({"code": 200, "message": "success", "data": None})

        with patch.object(app, "generate_username", return_value="user123"), patch.object(
            app, "http_post", side_effect=fake_post
        ):
            address, token = app.cloud_mail_create_address()

        self.assertEqual(address, "user123@mail.example.com")
        self.assertEqual(token, "test-token-uuid")
        self.assertEqual(captured["url"], "https://mail.example.com/api/public/addUser")
        self.assertEqual(captured["headers"]["Authorization"], "test-token-uuid")
        self.assertEqual(captured["json"]["list"][0]["email"], "user123@mail.example.com")

    def test_get_messages_posts_emaillist(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyResponse(
                {
                    "code": 200,
                    "data": [
                        {
                            "emailId": 1,
                            "toEmail": "a@mail.example.com",
                            "subject": "code",
                            "text": "Your code is 123456",
                            "content": "<div>123456</div>",
                        }
                    ],
                }
            )

        with patch.object(app, "http_post", side_effect=fake_post):
            messages = app.cloud_mail_get_messages(
                "https://mail.example.com", "tok", "a@mail.example.com"
            )

        self.assertEqual(len(messages), 1)
        self.assertEqual(captured["url"], "https://mail.example.com/api/public/emailList")
        self.assertEqual(captured["json"]["toEmail"], "a@mail.example.com")
        self.assertEqual(captured["headers"]["Authorization"], "tok")

    def test_get_email_and_token_routes_to_cloud_mail(self):
        with patch.object(
            app, "cloud_mail_create_address", return_value=("x@y.com", "tok")
        ) as mock_create:
            address, token = app.get_email_and_token()
        self.assertEqual((address, token), ("x@y.com", "tok"))
        mock_create.assert_called_once()

    def test_get_oai_code_extracts_code_from_email_text(self):
        mail = [
            {
                "emailId": 7,
                "toEmail": "user@mail.example.com",
                "subject": "xAI confirmation code",
                "text": "Your code is ABC-123",
            }
        ]
        with patch.object(app, "cloud_mail_get_messages", return_value=mail), \
                patch.object(app, "sleep_with_cancel", return_value=None):
            code = app.cloud_mail_get_oai_code(
                "tok",
                "user@mail.example.com",
                timeout=10,
                poll_interval=1,
            )
        self.assertEqual(code, "ABC-123")

    def test_get_oai_code_skips_non_target_recipient(self):
        mail = [
            {
                "emailId": 1,
                "toEmail": "other@mail.example.com",
                "subject": "xAI confirmation code",
                "text": "ZZZ-999",
            }
        ]
        with patch.object(app, "cloud_mail_get_messages", return_value=mail), \
                patch.object(app, "sleep_with_cancel", return_value=None):
            with self.assertRaises(Exception) as ctx:
                app.cloud_mail_get_oai_code(
                    "tok",
                    "user@mail.example.com",
                    timeout=1,
                    poll_interval=0,
                )
        self.assertIn("未收到验证码", str(ctx.exception))

    def test_pick_list_payload_supports_nested_list(self):
        nested = {
            "code": 200,
            "data": {
                "list": [
                    {
                        "emailId": 2,
                        "toEmail": "a@mail.example.com",
                        "text": "ABC-123",
                    }
                ]
            },
        }
        messages = app._pick_list_payload(nested)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["emailId"], 2)

    def test_get_messages_parses_nested_data_list(self):
        def fake_post(url, **kwargs):
            return DummyResponse(
                {
                    "code": 200,
                    "data": {
                        "list": [
                            {
                                "emailId": 9,
                                "toEmail": "a@mail.example.com",
                                "subject": "code",
                                "text": "Your code is ABC-123",
                            }
                        ]
                    },
                }
            )

        with patch.object(app, "http_post", side_effect=fake_post):
            messages = app.cloud_mail_get_messages(
                "https://mail.example.com", "tok", "a@mail.example.com"
            )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["emailId"], 9)

    def test_cloud_mail_post_retries_with_bearer(self):
        calls = []

        def fake_post(url, **kwargs):
            calls.append(kwargs.get("headers", {}).get("Authorization"))
            if len(calls) == 1:
                return DummyResponse({"message": "unauthorized"}, status_code=401)
            return DummyResponse({"code": 200, "data": []})

        with patch.object(app, "http_post", side_effect=fake_post):
            resp = app.cloud_mail_post(
                "https://mail.example.com/api/public/emailList",
                {"toEmail": "a@mail.example.com"},
                token="raw-token",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls, ["raw-token", "Bearer raw-token"])


if __name__ == "__main__":
    unittest.main()
