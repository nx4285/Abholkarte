import os, re, json, time, sqlite3, math, threading

from datetime import datetime, timezone

from functools import wraps

from urllib.parse import urlparse, unquote

import requests

from bs4 import BeautifulSoup

from flask import Flask, jsonify, request, session, send_from_directory

from werkzeug.security import generate_password_hash, check_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.getenv('DATA_DIR', '/tmp/abholkarte')

os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, 'abholkarte.db')

app = Flask(__name__)

app.secret_key = os.getenv(

    'FLASK_SECRET_KEY',

    'abholkarte-local-secret-change-me'

)

KLAZ_API = 'https://api.kleinanzeigen-agent.de/api/v2/kleinanzeigen'

OSRM = 'https://router.project-osrm.org'

NOMINATIM = 'https://nominatim.openstreetmap.org'

UA = 'Abholkarte/1.0 personal route-planning app'

_geo_lock = threading.Lock()

_last_geo = 0.0

def db():

    con = sqlite3.connect(DB_PATH)

    con.row_factory = sqlite3.Row

    return con

def init_db():

    con = db()

    c = con.cursor()

    c.executescript('''

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

    ''')

    con.commit()

    con.close()

init_db()

def get_setting(key, default=''):

    con = db()

    row = con.execute(

        'SELECT value FROM settings WHERE key=?',

        (key,)

    ).fetchone()

    con.close()

    return row['value'] if row else default

def set_setting(key, value):

    con = db()

    con.execute(

        'INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',

        (key, value)

    )

    con.commit()

    con.close()

def configured_pin_hash():

    env = os.getenv('APP_PIN', '').strip()

    if env:

        return generate_password_hash(env)

    return get_setting('app_pin_hash', '')

def api_key():

    return (

        os.getenv('KLAZ_API_KEY', '').strip()

        or get_setting('klaz_api_key', '').strip()

    )

def now():

    return datetime.now(timezone.utc).isoformat()

def truthy(v):

    return str(v).lower() in (

        '1',

        'true',

        'yes',

        'on'

    )

def auth_required(fn):

    @wraps(fn)

    def wrap(*a, **kw):

        pin_hash = configured_pin_hash()

        if not pin_hash or session.get('ok'):

            return fn(*a, **kw)

        return jsonify({

            'error': 'PIN_REQUIRED'

        }), 401

    return wrap

def klaz_get(path, params=None):

    key = api_key()

    if not key:

        raise RuntimeError('KLAZ_API_KEY_MISSING')

    r = requests.get(

        KLAZ_API + path,

        params=params or {},

        headers={

            'klaz_key': key,

            'User-Agent': UA

        },

        timeout=25

    )

    if r.status_code >= 400:

        try:

            msg = (

                r.json().get('error_code')

                or r.json().get('message')

            )

        except Exception:

            msg = f'HTTP_{r.status_code}'

        raise RuntimeError(msg)

    return r.json()

def extract_ad_id(url):

    m = re.search(

        r'/([0-9]{8,})-[0-9]+-[0-9]+(?:[/?]|$)',

        url

    )

    if not m:

        m = re.search(

            r'/([0-9]{8,})(?:[/?]|$)',

            url

        )

    return m.group(1) if m else None

def clean_url(url):

    p = urlparse(url.strip())

    return (

        f'{p.scheme or "https"}://'

        f'{p.netloc or "www.kleinanzeigen.de"}'

        f'{p.path}'

    )

def geocode(q):

    global _last_geo

    q = (q or '').strip()

    if not q:

        return None

    con = db()

    row = con.execute(

        'SELECT * FROM geocache WHERE query=?',

        (q.lower(),)

    ).fetchone()

    con.close()

    if row:

        return {

            'lat': row['lat'],

            'lon': row['lon'],

            'display': row['display']

        }

    with _geo_lock:

        wait = max(

            0,

            1.05 - (time.time() - _last_geo)

        )

        if wait:

            time.sleep(wait)

        r = requests.get(

            NOMINATIM + '/search',

            params={

                'q': q,

                'format': 'jsonv2',

                'limit': 1,

                'countrycodes': 'de'

            },

            headers={

                'User-Agent': UA

            },

            timeout=20

        )

        _last_geo = time.time()

    if not r.ok or not r.json():

        return None

    x = r.json()[0]

    out = {

        'lat': float(x['lat']),

        'lon': float(x['lon']),

        'display': x.get(

            'display_name',

            q

        )

    }

    con = db()

    con.execute(

        'INSERT OR REPLACE INTO geocache VALUES(?,?,?,?,?)',

        (

            q.lower(),

            out['lat'],

            out['lon'],

            out['display'],

            now()

        )

    )

    con.commit()

    con.close()

    return out

def normalize_ad(a, source='api', source_ref=None):

    loc = a.get('location') or {}

    place = (

        loc.get('name')

        or loc.get('city')

        or ''

    )

    price = a.get('price') or {}

    amount = (

        price.get('amount')

        if isinstance(price, dict)

        else None

    )

    ptxt = (

        'VB'

        if (

            isinstance(price, dict)

            and price.get('price_type') == 'NEGOTIABLE'

        )

        else ''

    )

    if amount is not None:

        ptxt = (

            f"{amount:,.0f} €".replace(',', '.')

            + (

                " VB"

                if price.get('negotiable')

                else ''

            )

        )

    images = a.get('images') or []

    image = ''

    if images:

        image = (

            images[0].get('url')

            if isinstance(images[0], dict)

            else str(images[0])

        )

    lat = (

        loc.get('latitude')

        or loc.get('lat')

    )

    lon = (

        loc.get('longitude')

        or loc.get('lon')

    )

    if (

        (lat is None or lon is None)

        and place

    ):

        g = geocode(place)

        if g:

            lat = g['lat']

            lon = g['lon']

    return {

        'ad_id': str(

            a.get('ad_id') or ''

        ),

        'url': a.get('ad_url') or '',

        'title': a.get('title') or 'Kleinanzeige',

        'price_text': ptxt,

        'price_amount': amount,

        'place': place,

        'lat': lat,

        'lon': lon,

        'source': source,

        'source_ref': source_ref,

        'status': a.get('status') or 'ACTIVE',

        'deleted': (

            1

            if a.get('deleted')

            else 0

        ),

        'image_url': image,

        'created_at': (

            a.get('created_at')

            or now()

        ),

        'updated_at': now(),

        'last_checked': now()

    }

def upsert_ad(x):

    if (

        not x.get('url')

        and x.get('ad_id')

    ):

        x['url'] = (

            f'https://www.kleinanzeigen.de/'

            f's-{x["ad_id"]}'

        )

    con = db()

    c = con.cursor()

    if x.get('ad_id'):

        row = c.execute(

            'SELECT id FROM ads WHERE ad_id=?',

            (x['ad_id'],)

        ).fetchone()

    else:

        row = c.execute(

            'SELECT id FROM ads WHERE url=?',

            (x.get('url', ''),)

        ).fetchone()

    fields = [

        'ad_id',

        'url',

        'title',

        'price_text',

        'price_amount',

        'place',

        'lat',

        'lon',

        'source',

        'source_ref',

        'status',

        'deleted',

        'image_url',

        'created_at',

        'updated_at',

        'last_checked'

    ]

    if row:

        sets = ','.join(

            f'{f}=?'

            for f in fields

            if x.get(f) is not None

        )

        vals = [

            x.get(f)

            for f in fields

            if x.get(f) is not None

        ] + [row['id']]

        c.execute(

            f'UPDATE ads SET {sets} WHERE id=?',

            vals

        )

        rid = row['id']

    else:

        vals = [

            x.get(f)

            for f in fields

        ]

        c.execute(

            f'''

            INSERT INTO ads(

                {",".join(fields)}

            )

            VALUES(

                {",".join("?" for _ in fields)}

            )

            ''',

            vals

        )

        rid = c.lastrowid

    con.commit()

    out = dict(

        c.execute(

            'SELECT * FROM ads WHERE id=?',

            (rid,)

        ).fetchone()

    )

    con.close()

    return out

def parse_search_url(url):

    p = urlparse(url)

    path = unquote(p.path)

    last = path.rstrip('/').split('/')[-1]

    q = ''

    m = re.search(

        r'/s-[^/]+/([^/]+)/k0',

        path

    )

    if m:

        q = m.group(1).replace('-', ' ')

    else:

        m = re.search(

            r'/s-([^/]+)/k0',

            path

        )

        if m:

            q = m.group(1).replace('-', ' ')

    cat = (

        re.search(r'c(\d+)', last)

        or re.search(

            r'/c(\d+)(?:/|$)',

            path

        )

    )

    loc = re.search(

        r'l(\d+)',

        last

    )

    rad = re.search(

        r'r(\d+)',

        last

    )

    minp = None

    maxp = None

    pm = re.search(

        r'preis:(\d*):(\d*)',

        path

    )

    if pm:

        minp = (

            int(pm.group(1))

            if pm.group(1)

            else None

        )

        maxp = (

            int(pm.group(2))

            if pm.group(2)

            else None

        )

    return {

        'query': q,

        'category_id': (

            cat.group(1)

            if cat

            else None

        ),

        'location_id': (

            loc.group(1)

            if loc

            else None

        ),

        'distance': (

            int(rad.group(1))

            if rad

            else None

        ),

        'min_price': minp,

        'max_price': maxp

    }

def direct_fetch_search(url):

    if not truthy(

        os.getenv(

            'ENABLE_DIRECT_FETCH',

            'false'

        )

    ):

        raise RuntimeError(

            'DIRECT_FETCH_DISABLED'

        )

    r = requests.get(

        url,

        headers={

            'User-Agent':

            'Mozilla/5.0 ' + UA

        },

        timeout=25

    )

    r.raise_for_status()

    soup = BeautifulSoup(

        r.text,

        'html.parser'

    )

    out = []

    for a in soup.select(

        'article.aditem'

    ):

        link = a.select_one(

            'a.ellipsis, '

            'a[href*="/s-anzeige/"]'

        )

        if not link:

            continue

        href = link.get(

            'href',

            ''

        )

        if href.startswith('/'):

            href = (

                'https://www.kleinanzeigen.de'

                + href

            )

        title = (

            a.select_one('.ellipsis')

            or link

        ).get_text(

            ' ',

            strip=True

        )

        price = (

            a.select_one(

                '.aditem-main--middle--'

                'price-shipping--price'

            )

            or a.select_one(

                '[class*=price]'

            )

        )

        loc = a.select_one(

            '.aditem-main--top--left'

        )

        out.append({

            'ad_id':

                extract_ad_id(href) or '',

            'url':

                clean_url(href),

            'title':

                title,

            'price_text':

                price.get_text(

                    ' ',

                    strip=True

                )

                if price

                else '',

            'place':

                loc.get_text(

                    ' ',

                    strip=True

                )

                if loc

                else '',

            'source':

                'search-direct',

            'status':

                'ACTIVE',

            'deleted':

                0,

            'created_at':

                now(),

            'updated_at':

                now(),

            'last_checked':

                now()

        })

    return out

@app.get('/')

def home():

    return send_from_directory(

        '.',

        'index.html'

    )

@app.get('/manifest.webmanifest')

def manifest_file():

    return send_from_directory(

        '.',

        'manifest.webmanifest'

    )

@app.get('/sw.js')

def service_worker_file():

    return send_from_directory(

        '.',

        'sw.js',

        mimetype='application/javascript'

    )

@app.get('/icon-192.png')

def icon192():

    return send_from_directory(

        '.',

        'icon-192.png'

    )

@app.get('/icon-512.png')

def icon512():

    return send_from_directory(

        '.',

        'icon-512.png'

    )

@app.post('/api/login')

def login():

    pin_hash = configured_pin_hash()

    supplied = str(

        (request.json or {}).get(

            'pin',

            ''

        )

    )

    if (

        not pin_hash

        or check_password_hash(

            pin_hash,

            supplied

        )

    ):

        session['ok'] = True

        return jsonify({

            'ok': True

        })

    return jsonify({

        'error': 'Falsche PIN'

    }), 403

@app.get('/api/config')

def config():

    return jsonify({

        'needs_pin':

            bool(configured_pin_hash())

            and not session.get('ok'),

        'api_ready':

            bool(api_key()),

        'direct_fetch':

            truthy(

                os.getenv(

                    'ENABLE_DIRECT_FETCH',

                    'false'

                )

            )

    })

@app.get('/api/settings')

@auth_required

def get_settings_api():

    return jsonify({

        'api_ready':

            bool(api_key()),

        'pin_set':

            bool(configured_pin_hash()),

        'direct_fetch':

            truthy(

                os.getenv(

                    'ENABLE_DIRECT_FETCH',

                    'false'

                )

            )

    })

@app.post('/api/settings')

@auth_required

def save_settings_api():

    x = request.json or {}

    if (

        'klaz_api_key' in x

        and x['klaz_api_key']

    ):

        set_setting(

            'klaz_api_key',

            str(

                x['klaz_api_key']

            ).strip()

        )

    if (

        'app_pin' in x

        and str(

            x['app_pin']

        ).strip()

    ):

        pin = str(

            x['app_pin']

        ).strip()

        if len(pin) < 4:

            return jsonify({

                'error':

                'PIN muss mindestens '

                '4 Zeichen haben.'

            }), 400

        set_setting(

            'app_pin_hash',

            generate_password_hash(pin)

        )

        session['ok'] = True

    return jsonify({

        'ok': True,

        'api_ready': bool(api_key()),

        'pin_set':

            bool(configured_pin_hash())

    })

@app.get('/api/ads')

@auth_required

def list_ads():

    con = db()

    rows = [

        dict(x)

        for x in con.execute(

            '''

            SELECT *

            FROM ads

            ORDER BY

                deleted ASC,

                updated_at DESC

            '''

        )

    ]

    con.close()

    return jsonify(rows)

@app.post('/api/ads/manual')

@auth_required

def manual_ad():

    x = request.json or {}

    url = (

        clean_url(

            x.get('url', '')

        )

        if x.get('url')

        else ''

    )

    aid = extract_ad_id(url)

    lat = x.get('lat')

    lon = x.get('lon')

    place = x.get('place', '')

    if (

        (not lat or not lon)

        and place

    ):

        g = geocode(place)

        if g:

            lat = g['lat']

            lon = g['lon']

    return jsonify(

        upsert_ad({

            'ad_id': aid,

            'url': url,

            'title':

                x.get('title')

                or 'Kleinanzeige',

            'price_text':

                x.get('price_text', ''),

            'price_amount':

                None,

            'place':

                place,

            'lat':

                lat,

            'lon':

                lon,

            'source':

                'manual',

            'source_ref':

                None,

            'status':

                'ACTIVE',

            'deleted':

                0,

            'image_url':

                '',

            'created_at':

                now(),

            'updated_at':

                now(),

            'last_checked':

                now()

        })

    )

@app.post('/api/import/ad')

@auth_required

def import_ad():

    url = (

        request.json

        or {}

    ).get(

        'url',

        ''

    ).strip()

    aid = extract_ad_id(url)

    if not aid:

        return jsonify({

            'error':

                'Inserat-ID konnte '

                'aus dem Link nicht '

                'erkannt werden.'

        }), 400

    try:

        j = klaz_get(

            f'/ads/{aid}'

        )

        ad = (

            (

                j.get('data')

                or {}

            ).get('ad')

            or {}

        )

        ad.setdefault(

            'ad_id',

            aid

        )

        ad.setdefault(

            'ad_url',

            clean_url(url)

        )

        return jsonify(

            upsert_ad(

                normalize_ad(

                    ad,

                    'link'

                )

            )

        )

    except Exception as e:

        return jsonify({

            'error':

                str(e),

            'can_manual':

                True,

            'ad_id':

                aid,

            'url':

                clean_url(url)

        }), 400

@app.delete('/api/ads/<int:rid>')

@auth_required

def del_ad(rid):

    con = db()

    con.execute(

        'DELETE FROM ads WHERE id=?',

        (rid,)

    )

    con.commit()

    con.close()

    return jsonify({

        'ok': True

    })

@app.patch('/api/ads/<int:rid>')

@auth_required

def patch_ad(rid):

    x = request.json or {}

    allowed = {

        'note',

        'selected',

        'deleted',

        'status'

    }

    sets = []

    vals = []

    for k, v in x.items():

        if k in allowed:

            sets.append(

                f'{k}=?'

            )

            vals.append(v)

    if not sets:

        return jsonify({

            'ok': True

        })

    con = db()

    con.execute(

        f'''

        UPDATE ads

        SET

            {",".join(sets)},

            updated_at=?

        WHERE id=?

        ''',

        vals + [

            now(),

            rid

        ]

    )

    con.commit()

    con.close()

    return jsonify({

        'ok': True

    })

@app.post('/api/refresh')

@auth_required

def refresh_ads():

    con = db()

    rows = [

        dict(x)

        for x in con.execute(

            '''

            SELECT *

            FROM ads

            WHERE

                ad_id IS NOT NULL

                AND ad_id<>''

            '''

        )

    ]

    con.close()

    changed = 0

    errors = []

    for x in rows:

        try:

            j = klaz_get(

                f'/ads/{x["ad_id"]}/status'

            )

            st = (

                (

                    j.get('data')

                    or {}

                ).get('status')

                or {}

            )

            deleted = (

                1

                if (

                    st.get('deleted')

                    or st.get('status')

                    not in (

                        None,

                        'ACTIVE'

                    )

                )

                else 0

            )

            con = db()

            con.execute(

                '''

                UPDATE ads

                SET

                    status=?,

                    deleted=?,

                    last_checked=?,

                    updated_at=?

                WHERE id=?

                ''',

                (

                    st.get('status')

                    or 'UNKNOWN',

                    deleted,

                    now(),

                    now(),

                    x['id']

                )

            )

            con.commit()

            con.close()

            changed += 1

        except Exception as e:

            errors.append({

                'id':

                    x['id'],

                'error':

                    str(e)

            })

    return jsonify({

        'checked':

            changed,

        'errors':

            errors

    })

@app.get('/api/searches')

@auth_required

def searches():

    con = db()

    rows = [

        dict(x)

        for x in con.execute(

            '''

            SELECT *

            FROM searches

            ORDER BY id DESC

            '''

        )

    ]

    con.close()

    return jsonify(rows)

@app.post('/api/searches')

@auth_required

def add_search():

    x = request.json or {}

    url = x.get(

        'url',

        ''

    ).strip()

    parsed = (

        parse_search_url(url)

        if url

        else {}

    )

    q = (

        x.get('query')

        or parsed.get('query')

        or ''

    )

    cat = (

        x.get('category_id')

        or parsed.get('category_id')

    )

    loc = (

        x.get('location_id')

        or parsed.get('location_id')

    )

    dist = (

        x.get('distance')

        if x.get('distance') is not None

        else parsed.get('distance')

    )

    minp = (

        x.get('min_price')

        if x.get('min_price') is not None

        else parsed.get('min_price')

    )

    maxp = (

        x.get('max_price')

        if x.get('max_price') is not None

        else parsed.get('max_price')

    )

    con = db()

    cur = con.execute(

        '''

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

        VALUES(

            ?,?,?,?,?,?,?,?,?

        )

        ''',

        (

            x.get('name')

            or q

            or 'Suche',

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

            '''

            SELECT *

            FROM searches

            WHERE id=?

            ''',

            (sid,)

        ).fetchone()

    )

    con.close()

    return jsonify(row)

@app.delete('/api/searches/<int:sid>')

@auth_required

def del_search(sid):

    con = db()

    con.execute(

        '''

        DELETE FROM searches

        WHERE id=?

        ''',

        (sid,)

    )

    con.commit()

    con.close()

    return jsonify({

        'ok': True

    })

@app.post('/api/searches/<int:sid>/sync')

@auth_required

def sync_search(sid):

    con = db()

    s = con.execute(

        '''

        SELECT *

        FROM searches

        WHERE id=?

        ''',

        (sid,)

    ).fetchone()

    con.close()

    if not s:

        return jsonify({

            'error':

                'Suche nicht gefunden'

        }), 404

    s = dict(s)

    ads = []

    try:

        params = {

            'q':

                s['query'] or '',

            'size':

                min(

                    int(

                        (

                            request.json

                            or {}

                        ).get(

                            'size',

                            100

                        )

                    ),

                    100

                )

        }

        for k in (

            'category_id',

            'location_id',

            'distance',

            'min_price',

            'max_price'

        ):

            if s.get(k) not in (

                None,

                ''

            ):

                params[k] = s[k]

        j = klaz_get(

            '/search',

            params

        )

        ads = (

            (

                j.get('data')

                or {}

            ).get('ads')

            or []

        )

        out = [

            upsert_ad(

                normalize_ad(

                    a,

                    'search',

                    str(sid)

                )

            )

            for a in ads

        ]

    except Exception as e:

        if (

            s.get('url')

            and truthy(

                os.getenv(

                    'ENABLE_DIRECT_FETCH',

                    'false'

                )

            )

        ):

            raw = direct_fetch_search(

                s['url']

            )

            out = []

            for a in raw:

                a['source_ref'] = str(sid)

                if (

                    not a.get('lat')

                    and a.get('place')

                ):

                    g = geocode(

                        a['place']

                    )

                    if g:

                        a['lat'] = g['lat']

                        a['lon'] = g['lon']

                out.append(

                    upsert_ad(a)

                )

        else:

            return jsonify({

                'error': str(e)

            }), 400

    con = db()

    con.execute(

        '''

        UPDATE searches

        SET last_sync=?

        WHERE id=?

        ''',

        (

            now(),

            sid

        )

    )

    con.commit()

    con.close()

    return jsonify({

        'count': len(out)

    })

@app.post('/api/geocode')

@auth_required

def api_geocode():

    g = geocode(

        (

            request.json

            or {}

        ).get(

            'q',

            ''

        )

    )

    return jsonify(

        g or {}

    ), (

        200

        if g

        else 404

    )

@app.post('/api/route')

@auth_required

def route():

    x = request.json or {}

    start = x.get('start')

    end = x.get('end')

    ids = x.get('ad_ids') or []

    if isinstance(

        start,

        str

    ):

        start = geocode(start)

    if (

        isinstance(

            end,

            str

        )

        and end.strip()

    ):

        end = geocode(end)

    if not start:

        return jsonify({

            'error':

                'Startort nicht gefunden'

        }), 400

    con = db()

    qmarks = (

        ','.join(

            '?' * len(ids)

        )

        if ids

        else 'NULL'

    )

    rows = (

        [

            dict(r)

            for r in con.execute(

                f'''

                SELECT *

                FROM ads

                WHERE

                    id IN ({qmarks})

                    AND lat IS NOT NULL

                    AND lon IS NOT NULL

                ''',

                ids

            )

        ]

        if ids

        else []

    )

    con.close()

    pts = [

        {

            'lat':

                start['lat'],

            'lon':

                start['lon'],

            'label':

                'Start',

            'id':

                None

        }

    ] + [

        {

            'lat':

                r['lat'],

            'lon':

                r['lon'],

            'label':

                r['title'],

            'id':

                r['id']

        }

        for r in rows

    ]

    if end:

        pts.append({

            'lat':

                end['lat'],

            'lon':

                end['lon'],

            'label':

                'Ziel',

            'id':

                None

        })

    if len(pts) < 2:

        return jsonify({

            'error':

                'Mindestens eine '

                'Abholung auswählen.'

        }), 400

    coords = ';'.join(

        f"{p['lon']},{p['lat']}"

        for p in pts

    )

    params = {

        'source':

            'first',

        'destination':

            'last'

            if end

            else 'any',

        'roundtrip':

            'false'

            if end

            else 'true',

        'geometries':

            'geojson',

        'overview':

            'full',

        'steps':

            'false'

    }

    r = requests.get(

        f'{OSRM}/trip/v1/driving/{coords}',

        params=params,

        headers={

            'User-Agent': UA

        },

        timeout=30

    )

    if not r.ok:

        return jsonify({

            'error':

                'Routingdienst '

                'nicht erreichbar'

        }), 502

    j = r.json()

    trips = (

        j.get('trips')

        or []

    )

    if not trips:

        return jsonify({

            'error':

                'Keine Route gefunden'

        }), 400

    wps = (

        j.get('waypoints')

        or []

    )

    ordered = sorted(

        zip(

            wps,

            pts

        ),

        key=lambda z:

            z[0].get(

                'waypoint_index',

                0

            )

    )

    return jsonify({

        'distance_km':

            round(

                trips[0]['distance']

                / 1000,

                1

            ),

        'duration_min':

            round(

                trips[0]['duration']

                / 60

            ),

        'geometry':

            trips[0]['geometry'],

        'order':

            [

                {

                    'label':

                        p['label'],

                    'id':

                        p['id']

                }

                for _, p in ordered

            ]

    })

if __name__ == '__main__':

    app.run(

        host='0.0.0.0',

        port=int(

            os.getenv(

                'PORT',

                '8080'

            )

        ),

        debug=True

    )
