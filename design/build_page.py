"""Compone le pagine della webapp partendo dal foglio di stile del progetto grafico.

Il progetto dell'interfaccia (Material 3 Expressive, materiali Tesla) è nei file
design/*.dc.html. Da lì si prendono le regole CSS, così colori, forme e animazioni
restano identici al progetto. Struttura e logica sono in design/page.body.html e
design/login.body.html.

Uso: python3 design/build_page.py
"""

import re
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE.parent / "teslacharger"
FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto+Flex:opsz,wdth,wght@8..144,25..151,100..1000&display=swap">\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&display=block">'
)
HEAD = """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="TeslaCharger">
<meta name="theme-color" content="#f9f9f9" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0e0e0e" media="(prefers-color-scheme: dark)">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<title>TeslaCharger</title>
{fonts}
<style>
{css}
</style>
</head>
"""


def design_css(name: str) -> str:
    source = (HERE / name).read_text()
    return re.search(r"<style>\n(.*?)</style>", source, re.S).group(1).replace("&amp;", "&")


def build(design: str, body: str, out: Path) -> None:
    parts = (HERE / body).read_text().split("<!--CSS-->")
    css = design_css(design) + parts[0].strip() + "\n"
    out.write_text(HEAD.format(fonts=FONTS, css=css) + parts[1].lstrip())
    print(out.name, len(out.read_text()), "caratteri")


build("Main.dc.html", "page.body.html", OUT / "page.html")
build("Login.dc.html", "login.body.html", OUT / "static" / "login.html")
