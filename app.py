import os
import re
import json
import time
import sqlite3
import threading

from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash


# ------------------------------------------------------------
# GRUNDEINSTELLUNGEN
# ------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.getenv("DATA_DIR", "/tmp/abholkarte")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "abholkarte.db")

app = Flask(__name__)

app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    "abholkarte-local-secret-change-me"
)

KLAZ_API = "https://api.kleinanzeigen-agent.de/api/v2/kleinanzeigen"
OSRM = "https://router.project-osrm.org"
NOMINATIM = "https://nominatim.openstreetmap.org"

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.0 Mobile/15E148 Safari/604.1"
)

_geo_lock = threading.Lock()
_last_geo = 0.0


# ------------------------------------------------------------
# HILFSFUNKTIONEN
# ------------------------------------------------------------

def now():
    return datetime.now(timezone.utc).isoformat()


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def direct_fetch_enabled():
    return truthy(os.getenv("ENABLE_DIRECT_FETCH", "false"))


def api_fallback_enabled():
    return truthy(os.getenv("USE_API_FALLBACK", "false"))


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    c = con.cursor()

    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS ads(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id TEXT UNIQUE,
            url TEXT NOT NULL,
            title TEXT,
            price_text TEXT,
            price_amount REAL,
            place TEXT,
            lat REAL,
            lon REAL,
            source TEXT DEFAULT 'manual',
            source_ref TEXT,
            status TEXT DEFAULT 'ACTIVE',
            deleted INTEGER DEFAULT 0,
            image_url TEXT,
            created_at TEXT,
            updated_at TEXT,
            last_checked TEXT,
            note TEXT DEFAULT '',
            selected INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS searches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            url TEXT,
            query TEXT,
            category_id TEXT,
            location_id TEXT,
            distance INTEGER,
            min_price INTEGER,
            max_price INTEGER,
            enabled INTEGER DEFAULT 1,
            last_sync TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS geocache(
            query TEXT PRIMARY KEY,
            lat REAL,
            lon REAL,
            display TEXT,
            ts TEXT
        );

        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )

    con.commit()
    con.close()


init_db()


def get_setting(key, default=""):
    con = db()
    row = con.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,)
    ).fetchone()
    con.close()
    return row["value"] if row else default


def set_setting(key, value):
    con = db()
    con.execute(
        """
        INSERT OR REPLACE INTO settings(key,value)
        VALUES(?,?)
        """,
        (key, value)
    )
    con.commit()
    con.close()


def api_key():
    env = os.getenv("KLAZ_API_KEY", "").strip()
    if env:
        return env
    return get_setting("klaz_api_key", "").strip()


def configured_pin_hash():
    env = os.getenv("APP_PIN", "").strip()
    if env:
        return generate_password_hash(env)
    return get_setting("app_pin_hash", "")


def auth_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        pin_hash = configured_pin_hash()

        if not pin_hash or session.get("ok"):
            return fn(*args, **kwargs)

        return jsonify({"error": "PIN_REQUIRED"}), 401

    return wrap


# ------------------------------------------------------------
# URL-HILFEN
# ------------------------------------------------------------

def extract_ad_id(url):
    if not url:
        return None

    m = re.search(
        r"/([0-9]{8,})-[0-9]+-[0-9]+(?:[/?]|$)",
        url
    )

    if not m:
        m = re.search(
            r"/([0-9]{8,})(?:[/?]|$)",
            url
        )

    return m.group(1) if m else None


def clean_url(url):
    url = (url or "").strip()

    if not url:
        return ""

    p = urlparse(url)

    scheme = p.scheme or "https"
    host = p.netloc or "www.kleinanzeigen.de"

    return f"{scheme}://{host}{p.path}"


def extract_first_url(text):
    if not text:
        return ""

    m = re.search(
        r"https?://[^\s<>\"']+",
        str(text)
    )

    if not m:
        return ""

    url = m.group(0).strip()
    return url.rstrip(".,);]}>")


# ------------------------------------------------------------
# GEO-CODING
# ------------------------------------------------------------

def geocode(q):
    global _last_geo

    q = (q or "").strip()

    if not q:
        return None

    con = db()
    row = con.execute(
        """
        SELECT *
        FROM geocache
        WHERE query=?
        """,
        (q.lower(),)
    ).fetchone()
    con.close()

    if row:
        return {
            "lat": row["lat"],
            "lon": row["lon"],
            "display": row["display"]
        }

    with _geo_lock:
        wait = max(0, 1.05 - (time.time() - _last_geo))

        if wait:
            time.sleep(wait)

        r = requests.get(
            NOMINATIM + "/search",
            params={
                "q": q,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "de"
            },
            headers={"User-Agent": "Abholkarte/1.0"},
            timeout=20
        )

        _last_geo = time.time()

    if not r.ok or not r.json():
        return None

    x = r.json()[0]

    out = {
        "lat": float(x["lat"]),
        "lon": float(x["lon"]),
        "display": x.get("display_name", q)
    }

    con = db()
    con.execute(
        """
        INSERT OR REPLACE INTO geocache
        VALUES(?,?,?,?,?)
        """,
        (
            q.lower(),
            out["lat"],
            out["lon"],
            out["display"],
            now()
        )
    )
    con.commit()
    con.close()

    return out


# ------------------------------------------------------------
# API-DIENST â NUR OPTIONAL
# ------------------------------------------------------------

def klaz_get(path, params=None):
    key = api_key()

    if not key:
        raise RuntimeError("KLAZ_API_KEY_MISSING")

    r = requests.get(
        KLAZ_API + path,
        params=params or {},
        headers={
            "klaz_key": key,
            "User-Agent": UA
        },
        timeout=25
    )

    if r.status_code >= 400:
        try:
            j = r.json()
            msg = (
                j.get("error_code")
                or j.get("message")
                or f"HTTP_{r.status_code}"
            )
        except Exception:
            msg = f"HTTP_{r.status_code}"

        raise RuntimeError(msg)

    return r.json()


# ------------------------------------------------------------
# HTML-AUSWERTUNG
# ------------------------------------------------------------

def parse_json_ld(soup):
    found = []

    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"}
    ):
        raw = (
            script.string
            or script.get_text()
            or ""
        ).strip()

        if not raw:
            continue

        try:
            data = json.loads(raw)

            if isinstance(data, list):
                found.extend(data)
            else:
                found.append(data)

        except Exception:
            continue

    return found


def meta_content(soup, *names):
    for name in names:
        tag = soup.find(
            "meta",
            attrs={"property": name}
        )

        if not tag:
            tag = soup.find(
                "meta",
                attrs={"name": name}
            )

        if tag and tag.get("content"):
            return tag.get("content").strip()

    return ""


def clean_title(title):
    title = (title or "").strip()

    title = re.sub(
        r"\s+\|\s+Kleinanzeigen.*$",
        "",
        title,
        flags=re.I
    )

    return title.strip()


def parse_price_amount(text):
    if not text:
        return None

    cleaned = (
        str(text)
        .replace(".", "")
        .replace(",", ".")
    )

    m = re.search(
        r"([0-9]+(?:\.[0-9]+)?)",
        cleaned
    )

    if not m:
        return None

    try:
        return float(m.group(1))
    except Exception:
        return None


def parse_html_ad(url, html, source="shortcut-html"):
    url = clean_url(url)

    if not url:
        raise RuntimeError("NO_URL")

    if not html or len(str(html).strip()) < 50:
        raise RuntimeError("NO_HTML")

    soup = BeautifulSoup(
        str(html),
        "html.parser"
    )

    ad_id = extract_ad_id(url)

    title = clean_title(
        meta_content(
            soup,
            "og:title",
            "twitter:title"
        )
    )

    description = meta_content(
        soup,
        "og:description",
        "description"
    )

    image_url = meta_content(
        soup,
        "og:image",
        "twitter:image"
    )

    price_text = ""
    price_amount = None
    place = ""
    lat = None
    lon = None

    for obj in parse_json_ld(soup):
        if not isinstance(obj, dict):
            continue

        if not title:
            title = clean_title(
                obj.get("name", "")
            )

        offers = obj.get("offers")

        if isinstance(offers, dict):
            price = offers.get("price")
            currency = offers.get(
                "priceCurrency",
                "EUR"
            )

            if price is not None:
                try:
                    price_amount = float(
                        str(price).replace(",", ".")
                    )
                except Exception:
                    pass

                symbol = "â¬" if currency == "EUR" else currency
                price_text = f"{price} {symbol}"

        address = obj.get("address")

        if isinstance(address, dict):
            locality = address.get("addressLocality")
            postal = address.get("postalCode")
            region = address.get("addressRegion")

            parts = [
                x
                for x in (postal, locality, region)
                if x
            ]

            if parts:
                place = " ".join(parts)

        geo = obj.get("geo")

        if isinstance(geo, dict):
            try:
                lat = float(geo.get("latitude"))
                lon = float(geo.get("longitude"))
            except Exception:
                pass

        img = obj.get("image")

        if not image_url and img:
            if isinstance(img, list):
                image_url = str(img[0])
            elif isinstance(img, dict):
                image_url = img.get("url") or ""
            else:
                image_url = str(img)

    if not title:
        h1 = soup.find("h1")

        if h1:
            title = h1.get_text(
                " ",
                strip=True
            )

    if not title:
        title = "Kleinanzeige"

    if not price_text:
        price_selectors = [
            "#viewad-price",
            ".boxedarticle--price",
            "[class*=price]",
            "[data-testid*=price]"
        ]

        for selector in price_selectors:
            tag = soup.select_one(selector)

            if tag:
                txt = tag.get_text(
                    " ",
                    strip=True
                )

                if "â¬" in txt:
                    price_text = txt
                    break

    if price_text and price_amount is None:
        price_amount = parse_price_amount(
            price_text
        )

    if not place:
        place_selectors = [
            "#viewad-locality",
            ".boxedarticle--details",
            "[class*=location]",
            "[data-testid*=location]"
        ]

        for selector in place_selectors:
            tag = soup.select_one(selector)

            if tag:
                txt = tag.get_text(
                    " ",
                    strip=True
                )

                if txt:
                    place = txt
                    break

    if (lat is None or lon is None) and place:
        g = geocode(place)

        if g:
            lat = g["lat"]
            lon = g["lon"]

    return {
        "ad_id": ad_id or "",
        "url": url,
        "title": title,
        "price_text": price_text,
        "price_amount": price_amount,
        "place": place,
        "lat": lat,
        "lon": lon,
        "source": source,
        "source_ref": None,
        "status": "ACTIVE",
        "deleted": 0,
        "image_url": image_url,
        "created_at": now(),
        "updated_at": now(),
        "last_checked": now(),
        "description": description
    }


# ------------------------------------------------------------
# DIREKTER SERVERABRUF â OPTIONAL
# ------------------------------------------------------------

def request_kleinanzeigen(url):
    return requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept":
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language":
                "de-DE,de;q=0.9,en;q=0.5",
            "Cache-Control": "no-cache"
        },
        timeout=25,
        allow_redirects=True
    )


def page_is_removed(response, soup):
    if response.status_code in (404, 410):
        return True

    text = soup.get_text(
        " ",
        strip=True
    ).lower()

    removed_signals = [
        "anzeige ist nicht mehr verfÃ¼gbar",
        "diese anzeige ist nicht mehr verfÃ¼gbar",
        "anzeige wurde gelÃ¶scht",
        "diese anzeige wurde gelÃ¶scht",
        "anzeige existiert nicht mehr",
        "angebot ist nicht mehr verfÃ¼gbar",
        "dieses angebot ist nicht mehr verfÃ¼gbar"
    ]

    return any(
        signal in text
        for signal in removed_signals
    )


def parse_direct_ad(url):
    if not direct_fetch_enabled():
        raise RuntimeError("DIRECT_FETCH_DISABLED")

    url = clean_url(url)
    response = request_kleinanzeigen(url)

    if response.status_code in (403, 429):
        raise RuntimeError(
            f"KLEINANZEIGEN_BLOCKED_{response.status_code}"
        )

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    if page_is_removed(response, soup):
        raise RuntimeError("AD_NOT_AVAILABLE")

    if response.status_code >= 400:
        raise RuntimeError(
            f"KLEINANZEIGEN_HTTP_{response.status_code}"
        )

    return parse_html_ad(
        response.url,
        response.text,
        source="direct"
    )


# ------------------------------------------------------------
# ANZEIGEN-DATENBANK
# ------------------------------------------------------------

def upsert_ad(x):
    if not x.get("url") and x.get("ad_id"):
        x["url"] = (
            "https://www.kleinanzeigen.de/"
            f"s-{x['ad_id']}"
        )

    con = db()
    c = con.cursor()

    row = None

    if x.get("ad_id"):
        row = c.execute(
            """
            SELECT id
            FROM ads
            WHERE ad_id=?
            """,
            (x["ad_id"],)
        ).fetchone()

    if not row and x.get("url"):
        row = c.execute(
            """
            SELECT id
            FROM ads
            WHERE url=?
            """,
            (x["url"],)
        ).fetchone()

    fields = [
        "ad_id",
        "url",
        "title",
        "price_text",
        "price_amount",
        "place",
        "lat",
        "lon",
        "source",
        "source_ref",
        "status",
        "deleted",
        "image_url",
        "created_at",
        "updated_at",
        "last_checked"
    ]

    if row:
        update_fields = [
            f for f in fields
            if x.get(f) is not None
        ]

        sets = ",".join(
            f"{f}=?"
            for f in update_fields
        )

        vals = [
            x.get(f)
            for f in update_fields
        ]

        vals.append(row["id"])

        c.execute(
            f"""
            UPDATE ads
            SET {sets}
            WHERE id=?
            """,
            vals
        )

        rid = row["id"]

    else:
        vals = [
            x.get(f)
            for f in fields
        ]

        marks = ",".join(
            "?"
            for _ in fields
        )

        c.execute(
            f"""
            INSERT INTO ads(
                {",".join(fields)}
            )
            VALUES({marks})
            """,
            vals
        )

        rid = c.lastrowid

    con.commit()

    out = dict(
        c.execute(
            """
            SELECT *
            FROM ads
            WHERE id=?
            """,
            (rid,)
        ).fetchone()
    )

    con.close()
    return out


# ------------------------------------------------------------
# API-DATEN NORMALISIEREN
# ------------------------------------------------------------

def normalize_api_ad(
    a,
    source="api",
    source_ref=None
):
    loc = a.get("location") or {}
    place = (
        loc.get("name")
        or loc.get("city")
        or ""
    )

    price = a.get("price") or {}

    amount = None
    price_text = ""

    if isinstance(price, dict):
        amount = price.get("amount")

        if amount is not None:
            price_text = (
                f"{amount:,.0f} â¬"
                .replace(",", ".")
            )

            if price.get("negotiable"):
                price_text += " VB"

    images = a.get("images") or []
    image = ""

    if images:
        if isinstance(images[0], dict):
            image = images[0].get("url") or ""
        else:
            image = str(images[0])

    lat = loc.get("latitude") or loc.get("lat")
    lon = loc.get("longitude") or loc.get("lon")

    if (lat is None or lon is None) and place:
        g = geocode(place)

        if g:
            lat = g["lat"]
            lon = g["lon"]

    return {
        "ad_id": str(a.get("ad_id") or ""),
        "url": a.get("ad_url") or "",
        "title": a.get("title") or "Kleinanzeige",
        "price_text": price_text,
        "price_amount": amount,
        "place": place,
        "lat": lat,
        "lon": lon,
        "source": source,
        "source_ref": source_ref,
        "status": a.get("status") or "ACTIVE",
        "deleted": 1 if a.get("deleted") else 0,
        "image_url": image,
        "created_at": a.get("created_at") or now(),
        "updated_at": now(),
        "last_checked": now()
    }


# ------------------------------------------------------------
# SUCHLINK ANALYSIEREN
# ------------------------------------------------------------

def parse_search_url(url):
    p = urlparse(url)
    path = unquote(p.path)

    last = path.rstrip("/").split("/")[-1]

    q = ""

    m = re.search(
        r"/s-[^/]+/([^/]+)/k0",
        path
    )

    if m:
        q = m.group(1).replace("-", " ")
    else:
        m = re.search(
            r"/s-([^/]+)/k0",
            path
        )

        if m:
            q = m.group(1).replace("-", " ")

    cat = (
        re.search(r"c(\d+)", last)
        or re.search(
            r"/c(\d+)(?:/|$)",
            path
        )
    )

    loc = re.search(r"l(\d+)", last)
    rad = re.search(r"r(\d+)", last)

    minp = None
    maxp = None

    pm = re.search(
        r"preis:(\d*):(\d*)",
        path
    )

    if pm:
        if pm.group(1):
            minp = int(pm.group(1))

        if pm.group(2):
            maxp = int(pm.group(2))

    return {
        "query": q,
        "category_id":
            cat.group(1)
            if cat
            else None,
        "location_id":
            loc.group(1)
            if loc
            else None,
        "distance":
            int(rad.group(1))
            if rad
            else None,
        "min_price": minp,
        "max_price": maxp
    }


# ------------------------------------------------------------
# WEBSEITE / DATEIEN
# ------------------------------------------------------------

@app.get("/")
def home():
    return send_from_directory(
        ".",
        "index.html"
    )


@app.get("/manifest.webmanifest")
def manifest_file():
    return send_from_directory(
        ".",
        "manifest.webmanifest"
    )


@app.get("/sw.js")
def service_worker_file():
    return send_from_directory(
        ".",
        "sw.js",
        mimetype="application/javascript"
    )


@app.get("/icon-192.png")
def icon192():
    return send_from_directory(
        ".",
        "icon-192.png"
    )


@app.get("/icon-512.png")
def icon512():
    return send_from_directory(
        ".",
        "icon-512.png"
    )


# ------------------------------------------------------------
# LOGIN
# ------------------------------------------------------------

@app.post("/api/login")
def login():
    pin_hash = configured_pin_hash()

    supplied = str(
        (request.json or {}).get(
            "pin",
            ""
        )
    )

    if (
        not pin_hash
        or check_password_hash(
            pin_hash,
            supplied
        )
    ):
        session["ok"] = True
        return jsonify({"ok": True})

    return jsonify(
        {"error": "Falsche PIN"}
    ), 403


# ------------------------------------------------------------
# KONFIGURATION
# ------------------------------------------------------------

@app.get("/api/config")
def config():
    return jsonify(
        {
            "needs_pin":
                bool(configured_pin_hash())
                and not session.get("ok"),
            "api_ready":
                bool(api_key()),
            "direct_fetch":
                direct_fetch_enabled(),
            "api_fallback":
                api_fallback_enabled()
        }
    )


@app.get("/api/settings")
@auth_required
def get_settings_api():
    return jsonify(
        {
            "api_ready":
                bool(api_key()),
            "pin_set":
                bool(configured_pin_hash()),
            "direct_fetch":
                direct_fetch_enabled(),
            "api_fallback":
                api_fallback_enabled()
        }
    )


@app.post("/api/settings")
@auth_required
def save_settings_api():
    x = request.json or {}

    if (
        "klaz_api_key" in x
        and x["klaz_api_key"]
    ):
        set_setting(
            "klaz_api_key",
            str(x["klaz_api_key"]).strip()
        )

    if (
        "app_pin" in x
        and str(x["app_pin"]).strip()
    ):
        pin = str(x["app_pin"]).strip()

        if len(pin) < 4:
            return jsonify(
                {
                    "error":
                        "PIN muss mindestens "
                        "4 Zeichen haben."
                }
            ), 400

        set_setting(
            "app_pin_hash",
            generate_password_hash(pin)
        )

        session["ok"] = True

    return jsonify(
        {
            "ok": True,
            "api_ready":
                bool(api_key()),
            "pin_set":
                bool(configured_pin_hash())
        }
    )


# ------------------------------------------------------------
# ANZEIGEN
# ------------------------------------------------------------

@app.get("/api/ads")
@auth_required
def list_ads():
    con = db()

    rows = [
        dict(x)
        for x in con.execute(
            """
            SELECT *
            FROM ads
            ORDER BY
                deleted ASC,
                updated_at DESC
            """
        )
    ]

    con.close()
    return jsonify(rows)


@app.post("/api/ads/manual")
@auth_required
def manual_ad():
    x = request.json or {}

    url = (
        clean_url(x.get("url", ""))
        if x.get("url")
        else ""
    )

    aid = extract_ad_id(url)

    lat = x.get("lat")
    lon = x.get("lon")
    place = x.get("place", "")

    if (not lat or not lon) and place:
        g = geocode(place)

        if g:
            lat = g["lat"]
            lon = g["lon"]

    item = {
        "ad_id": aid,
        "url": url,
        "title":
            x.get("title")
            or "Kleinanzeige",
        "price_text":
            x.get("price_text", ""),
        "price_amount": None,
        "place": place,
        "lat": lat,
        "lon": lon,
        "source": "manual",
        "source_ref": None,
        "status": "ACTIVE",
        "deleted": 0,
        "image_url": "",
        "created_at": now(),
        "updated_at": now(),
        "last_checked": now()
    }

    return jsonify(upsert_ad(item))


# ------------------------------------------------------------
# NEU: HTML-IMPORT VOM IPHONE / KURZBEFEHL
# KEINE CREDITS, KEIN BELMO-ABRUF BEI KLEINANZEIGEN
# ------------------------------------------------------------

@app.post("/api/import/html")
@auth_required
def import_html():
    x = request.json or {}

    raw_url = x.get("url") or ""

    url = (
        extract_first_url(raw_url)
        or raw_url
    )

    url = clean_url(url)

    html = x.get("html") or ""

    if not url:
        return jsonify(
            {"error": "Kein Link angegeben."}
        ), 400

    if not html:
        return jsonify(
            {"error": "Kein HTML empfangen."}
        ), 400

    try:
        item = parse_html_ad(
            url,
            html,
            source="shortcut-html"
        )

        return jsonify(upsert_ad(item))

    except Exception as e:
        return jsonify(
            {"error": str(e)}
        ), 400


# ------------------------------------------------------------
# KLASSISCHER IMPORT â NUR OPTIONAL
# ------------------------------------------------------------

@app.post("/api/import/ad")
@auth_required
def import_ad():
    url = (
        (request.json or {})
        .get("url", "")
        .strip()
    )

    if not url:
        return jsonify(
            {"error": "Kein Link angegeben."}
        ), 400

    aid = extract_ad_id(url)

    if not aid:
        return jsonify(
            {
                "error":
                    "Inserat-ID konnte "
                    "aus dem Link nicht "
                    "erkannt werden."
            }
        ), 400

    direct_error = None

    if direct_fetch_enabled():
        try:
            item = parse_direct_ad(url)
            return jsonify(upsert_ad(item))
        except Exception as e:
            direct_error = str(e)

    if (
        api_fallback_enabled()
        and api_key()
    ):
        try:
            j = klaz_get(
                f"/ads/{aid}"
            )

            ad = (
                (j.get("data") or {})
                .get("ad")
                or {}
            )

            ad.setdefault("ad_id", aid)
            ad.setdefault(
                "ad_url",
                clean_url(url)
            )

            item = normalize_api_ad(
                ad,
                "api-fallback"
            )

            return jsonify(upsert_ad(item))

        except Exception as e:
            return jsonify(
                {
                    "error": str(e),
                    "direct_error":
                        direct_error,
                    "can_manual": True
                }
            ), 400

    return jsonify(
        {
            "error":
                direct_error
                or (
                    "Serverabruf deaktiviert. "
                    "Bitte Ã¼ber den iPhone-"
                    "Kurzbefehl importieren."
                ),
            "can_manual": True,
            "ad_id": aid,
            "url": clean_url(url)
        }
    ), 400


@app.delete("/api/ads/<int:rid>")
@auth_required
def del_ad(rid):
    con = db()

    con.execute(
        """
        DELETE FROM ads
        WHERE id=?
        """,
        (rid,)
    )

    con.commit()
    con.close()

    return jsonify({"ok": True})


@app.patch("/api/ads/<int:rid>")
@auth_required
def patch_ad(rid):
    x = request.json or {}

    allowed = {
        "note",
        "selected",
        "deleted",
        "status"
    }

    sets = []
    vals = []

    for k, v in x.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)

    if not sets:
        return jsonify({"ok": True})

    con = db()

    con.execute(
        f"""
        UPDATE ads
        SET
            {",".join(sets)},
            updated_at=?
        WHERE id=?
        """,
        vals + [now(), rid]
    )

    con.commit()
    con.close()

    return jsonify({"ok": True})


# ------------------------------------------------------------
# STATUS-PRÃFUNG
# ------------------------------------------------------------

@app.post("/api/refresh")
@auth_required
def refresh_ads():
    if not direct_fetch_enabled():
        return jsonify(
            {
                "checked": 0,
                "removed": 0,
                "blocked": 0,
                "errors": [],
                "message":
                    "Direkte StatusprÃ¼fung ist "
                    "deaktiviert."
            }
        )

    return jsonify(
        {
            "checked": 0,
            "removed": 0,
            "blocked": 0,
            "errors": [],
            "message":
                "Direkte StatusprÃ¼fung aktuell "
                "nicht verwendet."
        }
    )


# ------------------------------------------------------------
# SUCHEN
# ------------------------------------------------------------

@app.get("/api/searches")
@auth_required
def searches():
    con = db()

    rows = [
        dict(x)
        for x in con.execute(
            """
            SELECT *
            FROM searches
            ORDER BY id DESC
            """
        )
    ]

    con.close()
    return jsonify(rows)


@app.post("/api/searches")
@auth_required
def add_search():
    x = request.json or {}

    url = x.get("url", "").strip()

    parsed = (
        parse_search_url(url)
        if url
        else {}
    )

    q = (
        x.get("query")
        or parsed.get("query")
        or ""
    )

    cat = (
        x.get("category_id")
        or parsed.get("category_id")
    )

    loc = (
        x.get("location_id")
        or parsed.get("location_id")
    )

    dist = (
        x.get("distance")
        if x.get("distance") is not None
        else parsed.get("distance")
    )

    minp = (
        x.get("min_price")
        if x.get("min_price") is not None
        else parsed.get("min_price")
    )

    maxp = (
        x.get("max_price")
        if x.get("max_price") is not None
        else parsed.get("max_price")
    )

    con = db()

    cur = con.execute(
        """
        INSERT INTO searches(
            name,
            url,
            query,
            category_id,
            location_id,
            distance,
            min_price,
            max_price,
            created_at
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            x.get("name")
            or q
            or "Suche",
            url,
            q,
            cat,
            loc,
            dist,
            minp,
            maxp,
            now()
        )
    )

    sid = cur.lastrowid
    con.commit()

    row = dict(
        con.execute(
            """
            SELECT *
            FROM searches
            WHERE id=?
            """,
            (sid,)
        ).fetchone()
    )

    con.close()
    return jsonify(row)


@app.delete("/api/searches/<int:sid>")
@auth_required
def del_search(sid):
    con = db()

    con.execute(
        """
        DELETE FROM searches
        WHERE id=?
        """,
        (sid,)
    )

    con.commit()
    con.close()

    return jsonify({"ok": True})


@app.post("/api/searches/<int:sid>/sync")
@auth_required
def sync_search(sid):
    return jsonify(
        {
            "error":
                "Kostenloser Suchlisten-Sync "
                "ist deaktiviert. Einzelanzeigen "
                "bitte per iPhone-Kurzbefehl "
                "importieren."
        }
    ), 400


# ------------------------------------------------------------
# GEO API
# ------------------------------------------------------------

@app.post("/api/geocode")
@auth_required
def api_geocode():
    g = geocode(
        (request.json or {}).get(
            "q",
            ""
        )
    )

    return jsonify(g or {}), (
        200 if g else 404
    )


# ------------------------------------------------------------
# ROUTENPLANUNG
# ------------------------------------------------------------

@app.post("/api/route")
@auth_required
def route():
    x = request.json or {}

    start = x.get("start")
    end = x.get("end")
    ids = x.get("ad_ids") or []

    if isinstance(start, str):
        start = geocode(start)

    if (
        isinstance(end, str)
        and end.strip()
    ):
        end = geocode(end)

    if not start:
        return jsonify(
            {
                "error":
                    "Startort nicht gefunden"
            }
        ), 400

    con = db()

    if ids:
        qmarks = ",".join(
            "?"
            for _ in ids
        )

        rows = [
            dict(r)
            for r in con.execute(
                f"""
                SELECT *
                FROM ads
                WHERE
                    id IN ({qmarks})
                    AND deleted=0
                    AND lat IS NOT NULL
                    AND lon IS NOT NULL
                """,
                ids
            )
        ]
    else:
        rows = []

    con.close()

    pts = [
        {
            "lat": start["lat"],
            "lon": start["lon"],
            "label": "Start",
            "id": None
        }
    ]

    for r in rows:
        pts.append(
            {
                "lat": r["lat"],
                "lon": r["lon"],
                "label": r["title"],
                "id": r["id"]
            }
        )

    if end:
        pts.append(
            {
                "lat": end["lat"],
                "lon": end["lon"],
                "label": "Ziel",
                "id": None
            }
        )

    if len(pts) < 2:
        return jsonify(
            {
                "error":
                    "Mindestens eine "
                    "Abholung auswÃ¤hlen."
            }
        ), 400

    coords = ";".join(
        f"{p['lon']},{p['lat']}"
        for p in pts
    )

    params = {
        "source": "first",
        "destination":
            "last" if end else "any",
        "roundtrip":
            "false" if end else "true",
        "geometries": "geojson",
        "overview": "full",
        "steps": "false"
    }

    r = requests.get(
        f"{OSRM}/trip/v1/driving/{coords}",
        params=params,
        headers={
            "User-Agent":
                "Abholkarte/1.0"
        },
        timeout=30
    )

    if not r.ok:
        return jsonify(
            {
                "error":
                    "Routingdienst "
                    "nicht erreichbar"
            }
        ), 502

    j = r.json()
    trips = j.get("trips") or []

    if not trips:
        return jsonify(
            {
                "error":
                    "Keine Route gefunden"
            }
        ), 400

    wps = j.get("waypoints") or []

    ordered = sorted(
        zip(wps, pts),
        key=lambda z:
            z[0].get(
                "waypoint_index",
                0
            )
    )

    return jsonify(
        {
            "distance_km":
                round(
                    trips[0]["distance"]
                    / 1000,
                    1
                ),
            "duration_min":
                round(
                    trips[0]["duration"]
                    / 60
                ),
            "geometry":
                trips[0]["geometry"],
            "order":
                [
                    {
                        "label": p["label"],
                        "id": p["id"]
                    }
                    for _, p in ordered
                ]
        }
    )


# ------------------------------------------------------------
# START
# ------------------------------------------------------------

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "8080"
            )
        ),
        debug=True
    )
