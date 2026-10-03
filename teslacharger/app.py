"""Avvio del servizio: ciclo di controllo e webapp nello stesso processo.

Uso: python3 -m teslacharger.app
Senza LIVE=true nel file .env il sistema scrive cosa farebbe ma non comanda l'auto.
"""

import logging
import os
import threading

from .config import Settings, load_env
from .controller import Controller
from .web import make_server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_env()
    settings = Settings.from_env()
    controller = Controller(settings)
    threading.Thread(target=controller.run_forever, daemon=True).start()

    host = os.environ.get("WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", 8787))
    logging.getLogger("teslacharger").info(
        "webapp su http://%s:%d, comandi all'auto %s",
        host, port, "attivi" if settings.live else "disattivati (modalità di prova)",
    )
    make_server(controller, host, port).serve_forever()


if __name__ == "__main__":
    main()
