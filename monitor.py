#!/usr/bin/env python3
"""
Monitor de medios panameños.
Busca una o varias palabras clave en noticias de medios de Panamá (vía Google Noticias)
y envía por WhatsApp (CallMeBot) las publicaciones nuevas.
"""
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# --- Configuración ---------------------------------------------------------

KEYWORDS = [k.strip() for k in (os.getenv("KEYWORDS") or "Sinfónica,Teatro Nacional,Teatro Balboa,Orquesta").split(",") if k.strip()]
PHONE = (os.getenv("WHATSAPP_PHONE") or "").strip()
APIKEY = (os.getenv("CALLMEBOT_APIKEY") or "").strip()
SEEN_FILE = os.getenv("SEEN_FILE") or "seen.json"
MAX_AGE_HOURS = 72   # ignora artículos más viejos que esto
KEEP_DAYS = 30       # cuánto tiempo recordar artículos ya avisados
MAX_MSG_CHARS = 1200

# Medios panameños a vigilar (dominio sin "www."). Agrega o quita a tu gusto.
SITES = [
    "prensa.com",
    "laestrella.com.pa",
    "panamaamerica.com.pa",
    "critica.com.pa",
    "elsiglo.com.pa",
    "metrolibre.com",
    "midiario.com",
    "tvn-2.com",
    "telemetro.com",
    "ecotvpanama.com",
    "nexpanama.com",
    "sertv.gob.pa",
    "radiopanama.com.pa",
    "ensegundos.com.pa",
    "newsroom.com.pa",
    "laverdadpanama.com",
]
SITES += [s.strip().lower() for s in (os.getenv("EXTRA_SITES") or "").split(",") if s.strip()]

UA = "Mozilla/5.0 (monitor-medios-panama)"

# --- Utilidades ------------------------------------------------------------

def norm(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()


def strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or ""))


def domain_of(url):
    d = urllib.parse.urlparse(url or "").netloc.lower()
    return d[4:] if d.startswith("www.") else d


def is_panama_source(url):
    d = domain_of(url)
    return any(d == s or d.endswith("." + s) for s in SITES)


def gnews_url(query):
    params = {"q": query, "hl": "es-419", "gl": "PA", "ceid": "PA:es-419"}
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def build_queries():
    queries = []
    for kw in KEYWORDS:
        queries.append((kw, f'"{kw}"'))
        for i in range(0, len(SITES), 4):  # agrupa sitios para no hacer demasiadas consultas
            chunk = SITES[i:i + 4]
            queries.append((kw, f'"{kw}" (' + " OR ".join(f"site:{s}" for s in chunk) + ")"))
    return queries


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def parse_items(xml_bytes):
    items = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        src = it.find("source")
        pub = it.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub) if pub else None
        except Exception:
            published = None
        title = (it.findtext("title") or "").strip()
        source_name = (src.text or "").strip() if src is not None else ""
        # Google añade " - Medio" al final del titular
        if source_name and title.endswith(" - " + source_name):
            title = title[: -len(" - " + source_name)]
        items.append({
            "title": title,
            "link": (it.findtext("link") or "").strip(),
            "summary": strip_tags(it.findtext("description") or ""),
            "source": source_name,
            "source_url": src.get("url", "") if src is not None else "",
            "published": published,
        })
    return items


def key_of(item):
    return norm(item["title"]) + "|" + domain_of(item["source_url"])

# --- Estado ----------------------------------------------------------------

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return None
    with open(SEEN_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_seen(seen):
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v) >= cutoff}
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1, sort_keys=True)

# --- WhatsApp --------------------------------------------------------------

def send_whatsapp(text):
    if not (PHONE and APIKEY):
        print("[sin credenciales, solo muestro el mensaje]\n" + text + "\n")
        return
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": PHONE, "text": text, "apikey": APIKEY})
    for intento in range(3):
        try:
            body = fetch(url).decode("utf-8", "ignore")
            if "error" in body.lower() and "queued" not in body.lower():
                print("CallMeBot respondió:", body[:300])
            return
        except Exception as e:
            print("Error enviando WhatsApp:", e)
            time.sleep(10)


def format_messages(items):
    header = f"🎻 Alertas de medios ({len(items)} nueva{'s' if len(items) != 1 else ''})\n"
    blocks = []
    for it in items:
        kws = it.get("kws") or KEYWORDS
        where = "titular" if any(norm(k) in norm(it["title"]) for k in kws) else "artículo"
        tags = ", ".join(kws)
        blocks.append(f"\n• [{tags}] *{it['source'] or domain_of(it['source_url'])}* ({where})\n{it['title']}\n{it['link']}\n")
    messages, current = [], header
    for b in blocks:
        if len(current) + len(b) > MAX_MSG_CHARS and current != header:
            messages.append(current)
            current = header + "(cont.)\n"
        current += b
    messages.append(current)
    return messages

# --- Principal -------------------------------------------------------------

def main():
    seen = load_seen()
    first_run = seen is None
    seen = seen or {}
    now = datetime.now(timezone.utc)
    max_age = now - timedelta(hours=MAX_AGE_HOURS)

    found = {}
    for kw, q in build_queries():
        try:
            items = parse_items(fetch(gnews_url(q)))
        except Exception as e:
            print(f"Fallo consultando {q!r}: {e}")
            continue
        for it in items:
            if not is_panama_source(it["source_url"]):
                continue
            if it["published"] and it["published"] < max_age:
                continue
            entry = found.setdefault(key_of(it), dict(it, kws=[]))
            if kw not in entry["kws"]:
                entry["kws"].append(kw)
        time.sleep(1)

    new = [it for k, it in found.items() if k not in seen]
    new.sort(key=lambda it: it["published"] or now)
    for k in found:
        seen.setdefault(k, now.isoformat())

    print(f"Encontrados: {len(found)} | nuevos: {len(new)} | primera ejecución: {first_run}")

    if first_run:
        send_whatsapp(f"✅ Monitor de medios activado para: {', '.join(KEYWORDS)}.\n"
                      f"Registré {len(found)} publicación(es) de los últimos {MAX_AGE_HOURS // 24} días; "
                      f"desde ahora te aviso solo lo nuevo.")
        if new:
            time.sleep(5)
            for msg in format_messages(new):
                send_whatsapp(msg)
                time.sleep(5)
    elif new:
        for msg in format_messages(new):
            send_whatsapp(msg)
            time.sleep(5)

    save_seen(seen)


if __name__ == "__main__":
    sys.exit(main())
