"""SMTP transport helpers."""

from __future__ import annotations

import smtplib
import ssl


def open_smtp_connection(
    server: str,
    port: int,
    encryption: str,
    ssl_context: ssl.SSLContext,
) -> smtplib.SMTP | smtplib.SMTP_SSL:
    """Open an SMTP connection with certificate verification when TLS is used."""
    if encryption == "ssl":
        smtp: smtplib.SMTP | smtplib.SMTP_SSL = smtplib.SMTP_SSL(
            server, port, timeout=30, context=ssl_context
        )
    else:
        smtp = smtplib.SMTP(server, port, timeout=30)

    smtp.ehlo()
    if encryption == "starttls":
        smtp.starttls(context=ssl_context)
        smtp.ehlo()
    return smtp
