"""Runtime regression tests for verified SMTP transport."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SMTP_PATH = ROOT / "custom_components/ha_tools_email/smtp.py"


def _load_smtp_module():
    spec = importlib.util.spec_from_file_location("ha_tools_email_smtp", SMTP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class VerifiedSmtpTransportTests(unittest.TestCase):
    def test_smtp_ssl_receives_verified_context(self) -> None:
        module = _load_smtp_module()
        context = object()
        smtp = Mock()

        with patch.object(module.smtplib, "SMTP_SSL", return_value=smtp) as ctor:
            result = module.open_smtp_connection(
                "smtp.example.com", 465, "ssl", context
            )

        self.assertIs(result, smtp)
        ctor.assert_called_once_with(
            "smtp.example.com", 465, timeout=30, context=context
        )
        smtp.ehlo.assert_called_once_with()
        smtp.starttls.assert_not_called()

    def test_starttls_receives_verified_context(self) -> None:
        module = _load_smtp_module()
        context = object()
        smtp = Mock()

        with patch.object(module.smtplib, "SMTP", return_value=smtp) as ctor:
            result = module.open_smtp_connection(
                "smtp.example.com", 587, "starttls", context
            )

        self.assertIs(result, smtp)
        ctor.assert_called_once_with("smtp.example.com", 587, timeout=30)
        smtp.starttls.assert_called_once_with(context=context)
        self.assertEqual(2, smtp.ehlo.call_count)

    def test_plain_smtp_does_not_start_tls(self) -> None:
        module = _load_smtp_module()
        smtp = Mock()

        with patch.object(module.smtplib, "SMTP", return_value=smtp) as ctor:
            result = module.open_smtp_connection(
                "smtp.lan", 25, "none", object()
            )

        self.assertIs(result, smtp)
        ctor.assert_called_once_with("smtp.lan", 25, timeout=30)
        smtp.starttls.assert_not_called()
        smtp.ehlo.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
