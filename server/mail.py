import smtplib
from email.message import EmailMessage

from .config import get_bool_env, get_env, get_int_env


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
        self.host = host or get_env("EOOVE_SMTP_HOST")
        self.port = port or get_int_env("EOOVE_SMTP_PORT", 587)
        self.username = username if username is not None else get_env("EOOVE_SMTP_USERNAME")
        self.password = password if password is not None else get_env("EOOVE_SMTP_PASSWORD")
        self.sender = sender or get_env("EOOVE_MAIL_FROM", "eoove@example.local")
        self.use_tls = get_bool_env("EOOVE_SMTP_TLS", True) if use_tls is None else bool(use_tls)
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
