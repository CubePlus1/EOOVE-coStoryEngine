import os
import smtplib
from email.message import EmailMessage


class NullMailTransport:
    configured = False

    def send(self, message):
        return None


class SmtpMailTransport(NullMailTransport):
    def __init__(
        self,
        host=None,
        port=None,
        username=None,
        password=None,
        sender=None,
        use_tls=None,
    ):
        self.host = host or os.environ.get("EOOVE_SMTP_HOST")
        self.port = int(port or os.environ.get("EOOVE_SMTP_PORT", "587"))
        self.username = username if username is not None else os.environ.get("EOOVE_SMTP_USERNAME")
        self.password = password if password is not None else os.environ.get("EOOVE_SMTP_PASSWORD")
        self.sender = sender or os.environ.get("EOOVE_MAIL_FROM", "eoove@example.local")
        tls_value = os.environ.get("EOOVE_SMTP_TLS", "1") if use_tls is None else str(int(bool(use_tls)))
        self.use_tls = tls_value not in {"0", "false", "False", "no", "NO"}
        self.configured = bool(self.host)

    def send(self, message):
        if not self.configured:
            return None
        email = EmailMessage()
        email["From"] = self.sender
        email["To"] = message["to"]
        email["Subject"] = message["subject"]
        email.set_content(message["body"])
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(email)
        return {"messageId": email["Message-ID"]}
