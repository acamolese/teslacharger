"""Webapp: stato, modalità, pannello dell'auto e notifiche. Accesso con password."""

import json
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .auth import COOKIE, SESSION_SECONDS, Auth
from .controller import Controller, data_dir
from .policy import Mode

HERE = Path(__file__).parent
STATIC = HERE / "static"
# File serviti senza accesso: servono al telefono per installare l'app e ricevere le notifiche
PUBLIC = {
    "/sw.js": "text/javascript",
    "/manifest.webmanifest": "application/manifest+json",
    "/apple-touch-icon.png": "image/png",
    "/icon-192.png": "image/png",
    "/icon-512.png": "image/png",
}


def make_server(controller: Controller, host: str, port: int) -> ThreadingHTTPServer:
    auth = Auth(data_dir())

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str, headers=()) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict, headers=()) -> None:
            self._send(status, json.dumps(data).encode(), "application/json", headers)

        def _token(self) -> str | None:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return cookie[COOKIE].value if COOKIE in cookie else None

        def _body(self) -> dict:
            if self.headers.get("Content-Type") != "application/json":
                raise ValueError("formato non valido")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 10_000:
                raise ValueError("richiesta troppo grande")
            return json.loads(self.rfile.read(length))

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in PUBLIC:
                return self._send(200, (STATIC / path[1:]).read_bytes(), PUBLIC[path])
            if path == "/login":
                return self._send(200, (STATIC / "login.html").read_bytes(), "text/html; charset=utf-8")
            if not auth.valid(self._token()):
                if path.startswith("/api/"):
                    return self._json(401, {"error": "accesso richiesto"})
                return self._send(302, b"", "text/plain", [("Location", "/login")])
            if path == "/":
                self._send(200, (HERE / "page.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/status":
                self._json(200, controller.snapshot())
            elif path == "/api/history":
                self._json(200, controller.history_summary())
            elif path == "/api/home":
                try:
                    self._json(200, controller.home_summary())
                except Exception as err:
                    self._json(502, {"error": str(err)})
            elif path == "/api/push/key":
                self._json(200, {"key": controller.push.public_key(), "devices": controller.push.count()})
            else:
                self._json(404, {"error": "non trovato"})

        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                data = self._body()
                if path == "/api/login":
                    return self._login(data)
                if not auth.valid(self._token()):
                    return self._json(401, {"error": "accesso richiesto"})
                result = self._action(path, data)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                return self._json(400, {"error": "richiesta non valida"})
            except RuntimeError as err:
                return self._json(502, {"error": str(err)})
            if result is None:
                return self._json(404, {"error": "non trovato"})
            self._json(200, result)

        def _login(self, data: dict) -> None:
            token = auth.login(str(data.get("password", "")))
            if token is None:
                # Rallenta i tentativi a raffica
                time.sleep(1.5)
                return self._json(403, {"error": "password non corretta"})
            cookie = f"{COOKIE}={token}; Max-Age={SESSION_SECONDS}; Path=/; HttpOnly; Secure; SameSite=Lax"
            self._json(200, {"ok": True}, [("Set-Cookie", cookie)])

        def _action(self, path: str, data: dict) -> dict | None:
            if path == "/api/mode":
                controller.set_mode(Mode(data["mode"]))
            elif path == "/api/grid":
                controller.set_grid_ok(bool(data["allow"]))
            elif path == "/api/target":
                percent = data.get("percent")
                controller.set_target(int(percent) if percent is not None else None, data.get("time") or None)
            elif path == "/api/car/refresh":
                return {"ok": controller.refresh_car()}
            elif path == "/api/push/subscribe":
                controller.push.subscribe(data)
            elif path == "/api/push/unsubscribe":
                controller.push.unsubscribe(str(data["endpoint"]))
            elif path == "/api/push/test":
                sent = controller.push.send("TeslaCharger", "Le notifiche funzionano.")
                return {"ok": sent > 0, "sent": sent}
            elif path == "/api/logout":
                auth.logout(self._token())
            else:
                return None
            return {"ok": True}

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)
