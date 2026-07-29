import json
import queue
import threading
import time
from urllib import error, request


class IKReplayLivePusher:
    """Best-effort async live-state pusher for the IK replay viewer."""

    def __init__(self, url, robot="h2", fps=10.0, timeout=0.2, logger=None):
        self.url = (url or "").strip()
        self.robot = (robot or "h2").strip().lower()
        self.interval = 1.0 / max(float(fps or 10.0), 0.1)
        self.timeout = max(float(timeout or 0.2), 0.05)
        self.logger = logger
        self._queue = queue.Queue(maxsize=1)
        self._running = bool(self.url)
        self._last_sent_at = 0.0
        self._last_error_log_at = 0.0
        self._thread = None
        if self._running:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    @property
    def enabled(self):
        return self._running

    def publish(self, payload):
        if not self._running:
            return
        now = time.monotonic()
        if now - self._last_sent_at < self.interval:
            return
        self._last_sent_at = now
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                pass

    def close(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run(self):
        while self._running:
            try:
                payload = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._post(payload)
            except (OSError, TimeoutError, error.URLError) as exc:
                self._log_error(exc)
            except Exception as exc:
                self._log_error(exc)

    def _post(self, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=self.timeout) as resp:
            resp.read(64)

    def _log_error(self, exc):
        if self.logger is None:
            return
        now = time.monotonic()
        if now - self._last_error_log_at < 10.0:
            return
        self._last_error_log_at = now
        try:
            self.logger.warning(f"IK replay live push failed, will keep teleop running: {exc}")
        except Exception:
            pass


def build_ik_replay_live_payload(
    *,
    robot,
    source,
    current_lr_arm_q,
    sol_q,
    timestamp=None,
    extra=None,
):
    current = current_lr_arm_q.tolist() if hasattr(current_lr_arm_q, "tolist") else list(current_lr_arm_q)
    target = sol_q.tolist() if hasattr(sol_q, "tolist") else list(sol_q)
    payload = {
        "robot": robot,
        "source": source,
        "timestamp": time.time() if timestamp is None else timestamp,
        "states": {
            "left_arm": {"qpos": current[:7]},
            "right_arm": {"qpos": current[-7:]},
        },
        "actions": {
            "left_arm": {"qpos": target[:7]},
            "right_arm": {"qpos": target[-7:]},
        },
        "current_lr_arm_q": current,
        "sol_q": target,
    }
    if extra:
        payload.update(extra)
    return payload
