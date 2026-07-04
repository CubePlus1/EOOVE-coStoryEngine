import threading
import time


class BackgroundRuntime:
    def __init__(
        self,
        game,
        beat_interval=30.0,
        print_interval=1.0,
        mail_interval=1.0,
        now_func=None,
        heartbeat_interval=None,
    ):
        self.game = game
        self.beat_interval = heartbeat_interval if heartbeat_interval is not None else beat_interval
        self.print_interval = print_interval
        self.mail_interval = mail_interval
        self.now_func = now_func or (lambda: int(time.time()))
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        if self.is_running():
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._beat_loop, name="eoove-beat", daemon=True),
            threading.Thread(target=self._print_loop, name="eoove-printer", daemon=True),
            threading.Thread(target=self._mail_loop, name="eoove-mailer", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout=None):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)

    def is_running(self):
        return any(thread.is_alive() for thread in self._threads)

    def wait_until_idle(self, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._print_pending_count() == 0 and self._beat_exists():
                return True
            time.sleep(min(self.beat_interval, self.print_interval, self.mail_interval, 0.01))
        return False

    def _beat_loop(self):
        while not self._stop.is_set():
            self.game.maybe_beat(now=self.now_func(), idle_seconds=self.beat_interval)
            self._stop.wait(self.beat_interval)

    def _print_loop(self):
        while not self._stop.is_set():
            self.game.process_next_print_job()
            self._stop.wait(self.print_interval)

    def _mail_loop(self):
        while not self._stop.is_set():
            self.game.process_next_mail_job()
            self._stop.wait(self.mail_interval)

    def _print_pending_count(self):
        with self.game.lock:
            return self.game.conn.execute(
                "SELECT COUNT(*) FROM print_queue WHERE status = 'pending'"
            ).fetchone()[0]

    def _beat_exists(self):
        with self.game.lock:
            row = self.game.conn.execute(
                "SELECT 1 FROM acts WHERE type = 'beat' LIMIT 1"
            ).fetchone()
        return row is not None


class WeaveWorkerRuntime:
    def __init__(self, game, slots=2, interval=0.01):
        self.game = game
        self.slots = max(1, int(slots))
        self.interval = interval
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        if self.is_running():
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._loop, name=f"eoove-weaver-{index + 1}", daemon=True)
            for index in range(self.slots)
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout=None):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)

    def is_running(self):
        return any(thread.is_alive() for thread in self._threads)

    def wait_until_idle(self, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.game.lock:
                pending = self.game.conn.execute(
                    "SELECT COUNT(*) FROM weave_queue WHERE status IN ('pending', 'running')"
                ).fetchone()[0]
            if pending == 0:
                return True
            time.sleep(0.01)
        return False

    def _loop(self):
        while not self._stop.is_set():
            self.game.process_next_weave_job()
            self._stop.wait(self.interval)


LocationWorkerRuntime = WeaveWorkerRuntime
