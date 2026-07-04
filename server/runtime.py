import queue
import threading
import time

from .constants import LOCATIONS


class BackgroundRuntime:
    def __init__(
        self,
        game,
        heartbeat_interval=1.0,
        print_interval=1.0,
        mail_interval=1.0,
        now_func=None,
    ):
        self.game = game
        self.heartbeat_interval = heartbeat_interval
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
            threading.Thread(target=self._heartbeat_loop, name="eoove-heartbeat", daemon=True),
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
            if self._print_pending_count() == 0 and self._heartbeat_exists():
                return True
            time.sleep(min(self.heartbeat_interval, self.print_interval, self.mail_interval, 0.01))
        return False

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            self.game.maybe_heartbeat(now=self.now_func())
            self._stop.wait(self.heartbeat_interval)

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

    def _heartbeat_exists(self):
        with self.game.lock:
            row = self.game.conn.execute(
                "SELECT 1 FROM acts WHERE type = 'heartbeat' LIMIT 1"
            ).fetchone()
        return row is not None


class LocationWorkerRuntime:
    def __init__(self, game):
        self.game = game
        self._stop = threading.Event()
        self._queues = {location["id"]: queue.Queue() for location in LOCATIONS}
        self._threads = []
        self._started = []
        self._started_condition = threading.Condition()

    def start(self, handler=None):
        if self.is_running():
            return
        self._stop.clear()
        handler = handler or self._default_handler
        self._threads = [
            threading.Thread(
                target=self._location_loop,
                args=(location["id"], handler),
                name=f"eoove-location-{location['id']}",
                daemon=True,
            )
            for location in LOCATIONS
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout=None):
        self._stop.set()
        for location_queue in self._queues.values():
            location_queue.put(None)
        for thread in self._threads:
            thread.join(timeout=timeout)

    def is_running(self):
        return any(thread.is_alive() for thread in self._threads)

    def enqueue(self, location, job):
        if location not in self._queues:
            raise ValueError(f"unknown location: {location}")
        self._queues[location].put(job)

    def wait_until_idle(self, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if all(location_queue.unfinished_tasks == 0 for location_queue in self._queues.values()):
                return True
            time.sleep(0.01)
        return False

    def wait_for_started(self, labels, timeout=2.0):
        labels = set(labels)
        deadline = time.time() + timeout
        with self._started_condition:
            while time.time() < deadline:
                if labels.issubset(set(self._started)):
                    return True
                remaining = max(0.0, deadline - time.time())
                self._started_condition.wait(timeout=min(remaining, 0.01))
        return labels.issubset(set(self._started))

    def _location_loop(self, location, handler):
        location_queue = self._queues[location]
        while not self._stop.is_set():
            job = location_queue.get()
            try:
                if job is None:
                    return
                self._mark_started(job)
                payload = handler(job)
                self._process_payload(payload)
            finally:
                location_queue.task_done()

    def _default_handler(self, job):
        return job

    def _process_payload(self, payload):
        if isinstance(payload, dict) and "_async" in payload:
            if hasattr(self.game, "complete_async_location_job"):
                self.game.complete_async_location_job(payload)
            else:
                self.game.process_async_location_job(payload)
            return
        self.game.act(payload)

    def _mark_started(self, job):
        label = job.get("label") if isinstance(job, dict) else None
        if label is None:
            return
        with self._started_condition:
            self._started.append(label)
            self._started_condition.notify_all()
