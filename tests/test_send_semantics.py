"""Test SMTP handoff semantics without importing Home Assistant."""

from __future__ import annotations

import ast
import logging
import unittest
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import Mock
import importlib.util

_spec = importlib.util.spec_from_file_location("send_history_test", Path(__file__).resolve().parents[1] / "custom_components/ha_tools_email/history.py")
_history = importlib.util.module_from_spec(_spec)
if _spec.origin and Path(_spec.origin).is_file():
    _spec.loader.exec_module(_history)


SOURCE = Path(__file__).resolve().parents[1] / "custom_components/ha_tools_email/__init__.py"


class SendSemanticsTests(unittest.TestCase):
    def _send_function(self, refused):
        module = ast.parse(SOURCE.read_text(encoding="utf-8"))
        node = next(item for item in module.body if isinstance(item, ast.FunctionDef) and item.name == "_send_email")
        fake_smtp = Mock()
        fake_smtp.sendmail.return_value = refused
        namespace = {
            "HomeAssistant": object,
            "PartialSMTPAcceptance": getattr(_history, "PartialSMTPAcceptance", ValueError),
            "_resolve_secret": lambda hass, value: value,
            "MIMEMultipart": MIMEMultipart,
            "MIMEText": MIMEText,
            "_LOGGER": logging.getLogger("test-smtp"),
            "open_smtp_connection": lambda *args: fake_smtp,
            "client_context": lambda: object(),
        }
        # Keep the actual MIME implementation and its pure Unicode boundary.
        composer_path = SOURCE.with_name("composer.py")
        composer = ast.parse(composer_path.read_text(encoding="utf-8"))
        text_node = next(item for item in composer.body
                         if isinstance(item, ast.FunctionDef) and item.name == "_mail_text")
        exec(compile(ast.Module(body=[text_node], type_ignores=[]), str(composer_path), "exec"), namespace)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
        return namespace["_send_email"], fake_smtp

    def test_local_marker_is_one_related_inline_part_and_legacy_html_stays_alternative(self):
        from email import message_from_string
        frame = {}
        exec(compile(SOURCE.with_name('mail_frame.py').read_text(), 'mail_frame.py', 'exec'), frame)
        marker=frame.get('ENERGY_MARKER_DATA_URI')
        self.assertIsInstance(marker,str)
        send,smtp=self._send_function({})
        send.__globals__['_energy_marker_mime']=frame['_energy_marker_mime']
        html='<html><body>'+('<img src="'+marker+'" alt="">')*25+'</body></html>'
        cfg={"server":"smtp.example.com","username":"user@example.com","password":"secret"}
        send(object(),cfg,'to@example.com','Subject','Body',html)
        msg=message_from_string(smtp.sendmail.call_args.args[2])
        self.assertEqual('multipart/alternative',msg.get_content_type())
        self.assertEqual('text/plain',msg.get_payload(0).get_content_type())
        related=msg.get_payload(1)
        self.assertEqual('multipart/related',related.get_content_type())
        self.assertEqual(2,len(related.get_payload()))
        image=related.get_payload(1)
        self.assertEqual('image/png',image.get_content_type())
        self.assertEqual('<ha-tools-energy-previous@local>',image['Content-ID'])
        self.assertEqual('inline',image['Content-Disposition'])
        import base64
        self.assertEqual(base64.b64decode(marker.split(',',1)[1]),image.get_payload(decode=True))
        sent=related.get_payload(0).get_payload(decode=True).decode()
        self.assertEqual(25,sent.count('cid:ha-tools-energy-previous@local'))
        self.assertNotIn('data:image',sent)
        smtp.sendmail.assert_called_once()
        smtp.reset_mock()
        send(object(),cfg,'to@example.com','Subject','Body','<p>Legacy HTML</p>')
        legacy=message_from_string(smtp.sendmail.call_args.args[2])
        self.assertEqual(['text/plain','text/html'],[part.get_content_type() for part in legacy.get_payload()])
        self.assertEqual(b'<p>Legacy HTML</p>',legacy.get_payload(1).get_payload(decode=True))

    def test_all_recipients_accepted_is_successful_handoff(self):
        send, smtp = self._send_function({})
        send(object(), {"server": "smtp.example.com", "username": "user@example.com", "password": "secret"}, "to@example.com", "Subject", "Body")
        smtp.sendmail.assert_called_once()

    def test_partial_refusal_is_not_reported_as_full_success(self):
        send, smtp = self._send_function({"refused@example.com": (550, b"No")})
        with self.assertRaisesRegex(ValueError, "some may already have been accepted") as error:
            send(object(), {"server": "smtp.example.com", "username": "user@example.com", "password": "secret"}, "ok@example.com, refused@example.com", "Subject", "Body")
        self.assertNotIn("refused@example.com", str(error.exception))
        smtp.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
