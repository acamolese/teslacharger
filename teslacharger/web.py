"""Webapp: pagina di stato e scelta della modalità. L'accesso è protetto da nginx."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .controller import Controller
from .policy import Mode

PAGE = Path(__file__).with_name("page.html")


def make_server(controller: Controller, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def do_GET(self):
            if self.path == "/":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                self._json(200, controller.snapshot())
            else:
                self._json(404, {"error": "non trovato"})

        def do_POST(self):
            if self.path not in ("/api/mode", "/api/grid", "/api/target"):
                return self._json(404, {"error": "non trovato"})
            # Solo richieste JSON partite dalla pagina stessa
            if self.headers.get("Content-Type") != "application/json":
                return self._json(415, {"error": "formato non valido"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length))
                if self.path == "/api/mode":
                    controller.set_mode(Mode(data["mode"]))
                elif self.path == "/api/grid":
                    controller.set_grid_ok(bool(data["allow"]))
                else:
                    percent = data.get("percent")
                    controller.set_target(
                        int(percent) if percent is not None else None, data.get("time") or None
                    )
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                return self._json(400, {"error": "richiesta non valida"})
            except RuntimeError as err:
                return self._json(502, {"error": str(err)})
            self._json(200, {"ok": True})

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)
