"""Accesso alla webapp: una sola password e una sessione duratura salvata in un cookie.

Si usa un cookie al posto dell'autenticazione del server web perché l'app
installata sulla schermata Home dell'iPhone deve restare collegata.
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

COOKIE = "tc_session"
SESSION_SECONDS = 365 * 24 * 3600
MAX_FAILURES = 8
LOCK_SECONDS = 15 * 60


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, data_dir: Path):
        self._password = os.environ.get("WEB_PASSWORD", "")
        self._file = data_dir / "sessions.json"
        self._lock = threading.Lock()
        self._failures = 0
        self._locked_until = 0.0

    def _sessions(self) -> dict:
        if not self._file.exists():
            return {}
        now = time.time()
        return {k: v for k, v in json.loads(self._file.read_text()).items() if v > now}

    def _store(self, sessions: dict) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(sessions))
        self._file.chmod(0o600)

    def login(self, password: str) -> str | None:
        """Restituisce il token di sessione, oppure None se la password è sbagliata."""
        with self._lock:
            if not self._password or time.time() < self._locked_until:
                return None
            if not hmac.compare_digest(password.encode(), self._password.encode()):
                self._failures += 1
                if self._failures >= MAX_FAILURES:
                    self._locked_until = time.time() + LOCK_SECONDS
                    self._failures = 0
                return None
            self._failures = 0
            token = secrets.token_urlsafe(32)
            sessions = self._sessions()
            sessions[_digest(token)] = time.time() + SESSION_SECONDS
            self._store(sessions)
            return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return _digest(token) in self._sessions()

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            sessions = self._sessions()
            sessions.pop(_digest(token), None)
            self._store(sessions)
