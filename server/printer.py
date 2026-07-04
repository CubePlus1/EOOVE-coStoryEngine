import json
import subprocess

from .config import get_env, get_float_env


class NullPrinterDriver:
    configured = False

    def print_ticket(self, ticket):
        return None


class CommandPrinterDriver(NullPrinterDriver):
    def __init__(self, command=None, timeout=None):
        self.command = command or get_env("EOOVE_PRINTER_COMMAND")
        self.timeout = timeout or get_float_env("EOOVE_PRINTER_TIMEOUT", 10)
        self.configured = bool(self.command)

    def print_ticket(self, ticket):
        if not self.configured:
            return None
        payload = json.dumps(ticket, ensure_ascii=False).encode("utf-8")
        completed = subprocess.run(
            self.command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            timeout=self.timeout,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(stderr or f"printer command failed: {completed.returncode}")
        return {
            "stdout": completed.stdout.decode("utf-8", errors="replace"),
            "returncode": completed.returncode,
        }
