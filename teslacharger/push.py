"""Notifiche push verso la webapp installata (Web Push con chiavi VAPID).

Richiede la libreria `cryptography`. Se manca, le notifiche restano disattivate
e il resto del sistema funziona comunque.
"""

import base64
import json
import logging
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("teslacharger")

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    AVAILABLE = True
except ImportError:  # pragma: no cover - dipende dall'ambiente
    AVAILABLE = False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _point(public_key) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """Cifra il messaggio per un'iscrizione, secondo lo schema aes128gcm (RFC 8291)."""
    client_point = _unb64(p256dh)
    client_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_point)
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_point = _point(server_key.public_key())
    shared = server_key.exchange(ec.ECDH(), client_key)

    ikm = _hkdf(_unb64(auth), shared, b"WebPush: info\x00" + client_point + server_point, 32)
    salt = os.urandom(16)
    key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(key).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack(">IB", 4096, len(server_point)) + server_point + ciphertext


class PushService:
    def __init__(self, data_dir: Path, subject: str):
        self._subject = subject
        self._subs_file = data_dir / "push-subscriptions.json"
        self._key_file = data_dir / "vapid-private.pem"
        self._lock = threading.Lock()
        self._key = None
        if AVAILABLE:
            self._key = self._load_key()

    @property
    def enabled(self) -> bool:
        return self._key is not None

    def _load_key(self):
        if self._key_file.exists():
            return serialization.load_pem_private_key(self._key_file.read_bytes(), password=None)
        key = ec.generate_private_key(ec.SECP256R1())
        self._key_file.parent.mkdir(parents=True, exist_ok=True)
        self._key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        self._key_file.chmod(0o600)
        return key

    def public_key(self) -> str | None:
        return _b64(_point(self._key.public_key())) if self._key else None

    def _subscriptions(self) -> list[dict]:
        if not self._subs_file.exists():
            return []
        return json.loads(self._subs_file.read_text())

    def _store(self, subs: list[dict]) -> None:
        self._subs_file.parent.mkdir(parents=True, exist_ok=True)
        self._subs_file.write_text(json.dumps(subs))
        self._subs_file.chmod(0o600)

    def count(self) -> int:
        with self._lock:
            return len(self._subscriptions())

    def subscribe(self, sub: dict) -> None:
        endpoint, keys = sub["endpoint"], sub["keys"]
        if not endpoint.startswith("https://"):
            raise ValueError("iscrizione non valida")
        entry = {"endpoint": endpoint, "p256dh": keys["p256dh"], "auth": keys["auth"]}
        with self._lock:
            subs = [s for s in self._subscriptions() if s["endpoint"] != endpoint]
            self._store(subs + [entry])

    def unsubscribe(self, endpoint: str) -> None:
        with self._lock:
            self._store([s for s in self._subscriptions() if s["endpoint"] != endpoint])

    def _vapid_header(self, endpoint: str) -> str:
        origin = urllib.parse.urlsplit(endpoint)
        claims = {
            "aud": f"{origin.scheme}://{origin.netloc}",
            "exp": int(time.time()) + 12 * 3600,
            "sub": self._subject,
        }
        signing_input = (
            _b64(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
            + "."
            + _b64(json.dumps(claims).encode())
        )
        r, s = decode_dss_signature(self._key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"vapid t={signing_input}.{_b64(signature)}, k={self.public_key()}"

    def send(self, title: str, body: str) -> int:
        """Invia la notifica a tutti i dispositivi iscritti. Restituisce quanti l'hanno accettata."""
        if not self.enabled:
            return 0
        payload = json.dumps({"title": title, "body": body}).encode()
        with self._lock:
            subs = self._subscriptions()
        delivered, expired = 0, []
        for sub in subs:
            request = urllib.request.Request(
                sub["endpoint"],
                data=encrypt(payload, sub["p256dh"], sub["auth"]),
                headers={
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "TTL": "3600",
                    "Urgency": "high",
                    "Authorization": self._vapid_header(sub["endpoint"]),
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=20):
                    delivered += 1
            except urllib.error.HTTPError as err:
                if err.code in (404, 410):
                    expired.append(sub["endpoint"])
                else:
                    log.warning("notifica non consegnata: HTTP %s %s", err.code, err.read()[:200])
            except OSError as err:
                log.warning("notifica non consegnata: %s", err)
        if expired:
            with self._lock:
                self._store([s for s in self._subscriptions() if s["endpoint"] not in expired])
        return delivered
