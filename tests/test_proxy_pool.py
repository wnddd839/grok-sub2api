"""proxy_pool 轮换逻辑单测。"""
from __future__ import annotations

import unittest

from proxy_pool import ProxyRotator, load_proxy_list, mask_proxy_url, normalize_proxy_url


class ProxyPoolTests(unittest.TestCase):
    def test_normalize_adds_scheme(self):
        self.assertEqual(normalize_proxy_url("1.2.3.4:8080"), "http://1.2.3.4:8080")

    def test_mask_hides_password(self):
        masked = mask_proxy_url("http://user:secret@host:8000")
        self.assertIn("user:***@", masked)
        self.assertNotIn("secret", masked)

    def test_load_merges_pool_file_and_single(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "p.txt"
            path.write_text(
                "http://a:1@h:1\n# comment\nhttp://b:2@h:2\n",
                encoding="utf-8",
            )
            urls = load_proxy_list(
                pool=["http://c:3@h:3"],
                pool_file=str(path),
                single="http://a:1@h:1",
            )
        self.assertEqual(
            urls,
            ["http://c:3@h:3", "http://a:1@h:1", "http://b:2@h:2"],
        )

    def test_sticky_until_accounts_per_ip(self):
        rot = ProxyRotator(
            ["http://p1:1", "http://p2:2"],
            accounts_per_ip=2,
            rotate_on_fail=True,
        )
        a = rot.acquire()
        self.assertEqual(a, "http://p1:1")
        self.assertEqual(rot.acquire(current=a), "http://p1:1")
        self.assertFalse(rot.record_success(a))
        self.assertEqual(rot.acquire(current=a), "http://p1:1")
        self.assertTrue(rot.record_success(a))
        b = rot.acquire(current=a)
        self.assertEqual(b, "http://p2:2")

    def test_fail_retires_proxy(self):
        rot = ProxyRotator(["http://p1:1", "http://p2:2"], accounts_per_ip=5)
        a = rot.acquire()
        rot.record_fail(a)
        b = rot.acquire(current=a)
        self.assertEqual(b, "http://p2:2")


if __name__ == "__main__":
    unittest.main()
