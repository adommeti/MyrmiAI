"""Write the digest to the archive and, optionally, email it.

The archive write is the contract: if email is misconfigured or SMTP is down,
the digest still exists at ``digests/<week>.md`` and is committed by CI. Email
is best-effort on top of that, never the only copy.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .config import DIGESTS_DIR, Settings, env
from .models import DigestRun

log = logging.getLogger(__name__)


def write_archive(run: DigestRun, markdown: str, html: str, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or DIGESTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{run.slug}.md"
    md_path.write_text(markdown, encoding="utf-8")
    (out_dir / f"{run.slug}.html").write_text(html, encoding="utf-8")
    log.info("Wrote %s", md_path)
    return md_path


def send_email(run: DigestRun, settings: Settings, html: str, markdown: str) -> bool:
    """Send the digest. Returns False (without raising) if it could not."""
    host = env("SMTP_HOST")
    user = env("SMTP_USER")
    password = env("SMTP_PASSWORD")
    recipient = env("DIGEST_TO") or user

    if not (host and user and password and recipient):
        log.info("SMTP is not configured; skipping email. The digest is in digests/.")
        return False

    message = EmailMessage()
    message["Subject"] = settings.subject_template.format(
        week_of=f"{run.period_start:%b %-d}", items=run.total_items
    )
    message["From"] = user
    message["To"] = recipient
    message.set_content(markdown)
    message.add_alternative(html, subtype="html")

    try:
        port = int(env("SMTP_PORT", "587"))
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=60) as smtp:
                smtp.login(user, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=60) as smtp:
                smtp.starttls()
                smtp.login(user, password)
                smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Email delivery failed: %s", exc)
        return False

    log.info("Emailed the digest to %s", recipient)
    return True
