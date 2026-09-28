import os
import requests
import json
import re
import datetime
import random
import string
import zipfile
import shutil
import tempfile
import secrets
import hashlib
import uuid
from pathlib import Path
from urllib.parse import urlencode
from functools import wraps
import subprocess
from flask import after_this_request
from flask_socketio import SocketIO
from flask import Flask, redirect, url_for, render_template, request, session, send_from_directory, jsonify, send_file
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from datetime import timedelta
from flask import Response

API_CACHE = {}

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# --- KONFIGURACJA ---
app.secret_key = "REDACTED
SYNC_TOKEN = "REDACTED
SUBDOMAINS_FILE = "subdomains.json"
SHARES_FILE = "shares.json"
VIDEO_SHARES_FILE = "video_shares.json"
GB = 1000*1000*1000
DROPS_FILE = "drops.json"
DROP_FOLDER = "drops"
DROP_EXPIRY_OPTIONS = {
    "1h": 3600,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "7d": 604800,
}
DROP_MAX_BYTES = 1*GB
DROP_ACCOUNT_MAX_BYTES = 5*GB
DROP_MAX_BYTES_ANON = 500*1000*1000
DROP_MAX_BYTES_AUTH = 1*GB
DROP_ACCOUNT_MAX_BYTES_ANON = 5*GB
DROP_ACCOUNT_MAX_BYTES_AUTH = 10*GB
os.makedirs(DROP_FOLDER, exist_ok=True)

def get_drop_owner_and_limits():
    """Zwraca (email, uid, is_auth, per_file_limit, account_limit) dla DROP.
    Dla zalogowanych: limity 1GB / 10GB, dla anon: 500MB / 5GB."""
    user = get_discord_user()
    if user and user.get('email') and user.get('id'):
        return user.get('email'), str(user.get('id')), True, DROP_MAX_BYTES_AUTH, DROP_ACCOUNT_MAX_BYTES_AUTH
    # anon: użyj session anon_drop_id
    anon_id = session.get('anon_drop_id')
    if not anon_id:
        anon_id = 'anon_' + secrets.token_urlsafe(8)
        session['anon_drop_id'] = anon_id
        session.permanent = True
    anon_email = f"{anon_id}@anon.local"
    return anon_email, anon_id, False, DROP_MAX_BYTES_ANON, DROP_ACCOUNT_MAX_BYTES_ANON

# Ustawienia sesji – 7 dni, SSO na *.dinguzhosting.online
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_DOMAIN'] = ".dinguzhosting.online"

# --- Nagłówki zabezpieczające (PO utworzeniu app) ---
@app.after_request
def add_security_headers(response):
    # Wyłączamy blokadę cache dla wideo, żeby player mógł płynnie buforować film!
    if request.path.startswith('/video/') or request.path.startswith('/dinguzplus/stream') or request.path.startswith('/dinguzplus/poster') or request.path.startswith('/dinguzplus/thumbnail'):
        response.headers['Cache-Control'] = 'public, max-age=86400'
        response.headers['Accept-Ranges'] = 'bytes'
    else:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
    return response

# Discord OAuth2 – dane aplikacji
DISCORD_CLIENT_ID = REDACTED
DISCORD_CLIENT_SECRET = "REDACTED
DISCORD_REDIRECT_URI = "REDACTED

PTERO_URL = "REDACTED
PTERO_APP_KEY = "REDACTED
PTERO_CLIENT_KEY = "REDACTED

CF_API_TOKEN = "REDACTED
CF_ZONE_ID = "REDACTED
NODE_PUBLIC_IP = "REDACTED

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

tempfile.tempdir = os.path.abspath(UPLOAD_FOLDER)

USER_QUOTA_LIMIT = 5 * 1000 * 1000 * 1000   # 5 GB

HOSTING_TIERS = {
    "free": 5*GB,
    "plus": 10*GB,
    "pro": 20*GB,
    "unlimited": 100*GB
}
HOSTING_TIERS_FILE = "hosting_tiers.json"
OWNER_DISCORD_ID = "REDACTED

def _load_hosting_tiers():
    if os.path.exists(HOSTING_TIERS_FILE):
        try:
            with open(HOSTING_TIERS_FILE,"r") as f:
                return json.load(f)
        except: return {}
    return {}
def _save_hosting_tiers(data):
    with open(HOSTING_TIERS_FILE,"w") as f:
        json.dump(data,f,indent=2)

def get_hosting_tier(email, discord_id=None):
    # owner always unlimited
    if discord_id and str(discord_id)==OWNER_DISCORD_ID:
        return "unlimited", None
    data=_load_hosting_tiers()
    # try by discord_id first, then email
    key = str(discord_id) if discord_id and str(discord_id) in data else email
    entry=data.get(key) or data.get(email) or {}
    tier=entry.get("tier","free")
    exp=entry.get("expires")
    if exp:
        try:
            if datetime.datetime.fromisoformat(exp) < datetime.datetime.now():
                # expired -> revert to free and save
                entry["tier"]="free"; entry["expires"]=None
                data[key]=entry; _save_hosting_tiers(data)
                return "free", None
        except: pass
    # also handle owner by email if discord_id not provided but email belongs to owner? need discord lookup - skip
    return tier, exp

def get_hosting_quota(email, discord_id=None):
    tier,_=get_hosting_tier(email, discord_id)
    return HOSTING_TIERS.get(tier, HOSTING_TIERS["free"]), tier

def _load_drops():
    if os.path.exists(DROPS_FILE):
        try:
            with open(DROPS_FILE,"r") as f:
                return json.load(f)
        except: return {}
    return {}
def _save_drops(data):
    with open(DROPS_FILE,"w") as f:
        json.dump(data,f,indent=2)
def _cleanup_expired_drops():
    data=_load_drops()
    now=datetime.datetime.now()
    changed=False
    for did, info in list(data.items()):
        exp_str=info.get("expiry")
        try:
            exp=datetime.datetime.fromisoformat(exp_str) if exp_str else now
        except: exp=now
        if now >= exp:
            p=info.get("path")
            if p and os.path.exists(p):
                try:
                    if os.path.isdir(p): shutil.rmtree(p)
                    else: os.remove(p)
                except: pass
            data.pop(did,None)
            changed=True
    if changed:
        _save_drops(data)
    return data

DROP_UPLOAD_SESSIONS = {}

def _get_drop_used(drop_path):
    total=0
    if not drop_path or not os.path.isdir(drop_path):
        return 0
    for root, dirs, fnames in os.walk(drop_path):
        dirs[:] = [d for d in dirs if not d.startswith(".tmp_")]
        for fn in fnames:
            if fn.startswith(".tmp_") or fn.endswith(".part") or fn==".meta.json":
                continue
            fp=os.path.join(root, fn)
            try:
                total+=os.path.getsize(fp)
            except: pass
    return total

def _get_drop_account_used(email, uid):
    drops=_load_drops()
    total=0
    for did, info in drops.items():
        if info.get("owner")!=email and info.get("owner_id")!=uid:
            continue
        p=info.get("path")
        total+=_get_drop_used(p)
    return total


socketio = SocketIO(app, cors_allowed_origins="*")

def set_pterodactyl_password(user_id, password):
    headers = {
        "Authorization": f"Bearer {PTERO_APP_KEY}",
        "Accept": "Application/vnd.pterodactyl.v1+json",
        "Content-Type": "application/json"
    }

    payload = {
        "password": password
    }

    try:
        response = requests.patch(
            f"{PTERO_URL}/api/application/users/{user_id}",
            headers=headers,
            json=payload,
            timeout=10
        )
    except requests.RequestException as e:
        print(f"[PTERO] Błąd zmiany hasła: {e}")
        return False

    if response.status_code != 200:
        print(
            f"[PTERO] Nie udało się zmienić hasła.\n"
            f"HTTP: {response.status_code}\n"
            f"Response: {response.text}"
        )
        return False

    print(f"[PTERO] Hasło użytkownika {user_id} zostało zmienione.")
    return True

# ==========================================
#   POMOCNICZE FUNKCJE
# ==========================================

def get_discord_user():
    """Pobiera dane użytkownika z sesji."""
    if 'discord_user' in session:
        return session['discord_user']

    token = session.get('discord_token')
    if not token:
        return None
    
    headers = {'Authorization': f"{token['token_type']} {token['access_token']}"}
    resp = requests.get('https://discord.com/api/users/@me', headers=headers)
    if resp.status_code == 200:
        user_data = resp.json()
        session['discord_user'] = user_data
        return user_data
    else:
        session.pop('discord_token', None)
        session.pop('discord_user', None)
        return None

def ensure_pterodactyl_user(discord_user):
    if not discord_user:
        print("[PTERO] Brak danych Discord użytkownika.")
        return False, None

    email = (discord_user.get("email") or "").strip().lower()

    if not email:
        print("[PTERO] Discord nie zwrócił adresu email.")
        return False, None

    headers = {
        "Authorization": f"Bearer {PTERO_APP_KEY}",
        "Accept": "Application/vnd.pterodactyl.v1+json",
        "Content-Type": "application/json"
    }

    # Sprawdzenie, czy użytkownik już istnieje
    try:
        response = requests.get(
            f"{PTERO_URL}/api/application/users",
            headers=headers,
            params={"filter[email]": email},
            timeout=10
        )
    except requests.RequestException as e:
        print(f"[PTERO] Błąd połączenia: {e}")
        return False, None

    if response.status_code != 200:
        print(
            f"[PTERO] Błąd sprawdzania użytkownika: "
            f"HTTP {response.status_code}: {response.text}"
        )
        return False, None

    try:
        users = response.json().get("data", [])
    except Exception:
        print("[PTERO] Nieprawidłowa odpowiedź JSON.")
        return False, None

    # Użytkownik już istnieje
	if users:
    	user = users[0].get("attributes", {})

    print(
        f"[PTERO] Użytkownik już istnieje: "
        f"{user.get('username')} ({email})"
    )

    return True, user, True

    # Generowanie username
    discord_username = (
        discord_user.get("username")
        or discord_user.get("global_name")
        or "user"
    )

    username = re.sub(
        r"[^a-zA-Z0-9_-]",
        "",
        discord_username
    )

    if not username:
        username = "user"

    discord_id = str(discord_user.get("id") or "")

    if discord_id:
        username = f"{username[:20]}_{discord_id[-6:]}"

    username = username[:32]

    # Tymczasowe losowe hasło
    temporary_password = secrets.token_urlsafe(32)

    payload = {
        "email": email,
        "username": username,
        "first_name": (
            discord_user.get("global_name")
            or discord_user.get("username")
            or "Dinguz"
        )[:191],
        "last_name": "User",
        "password": temporary_password,
        "root_admin": False
    }

    # Utworzenie użytkownika
    try:
        response = requests.post(
            f"{PTERO_URL}/api/application/users",
            headers=headers,
            json=payload,
            timeout=10
        )
    except requests.RequestException as e:
        print(f"[PTERO] Błąd tworzenia użytkownika: {e}")
        return False, None

    if response.status_code not in (200, 201):
        print(
            f"[PTERO] NIE UDAŁO SIĘ UTWORZYĆ UŻYTKOWNIKA\n"
            f"HTTP: {response.status_code}\n"
            f"Response: {response.text}"
        )
        return False, None

    try:
        user = response.json().get("attributes", {})
    except Exception:
        print("[PTERO] Nieprawidłowa odpowiedź po tworzeniu użytkownika.")
        return False, None

    print(
        f"[PTERO] UTWORZONO UŻYTKOWNIKA: "
        f"{user.get('username')} ({email})"
    )

    return True, user

@app.context_processor
def inject_discord_user():
    try:
        u = get_discord_user()
        return dict(discord_user=u)
    except:
        return dict(discord_user=None)

def _is_safe_next(next_url):
    if not next_url:
        return False
    try:
        from urllib.parse import urlparse
        p = urlparse(next_url)
        # allow relative url
        if not p.netloc:
            return next_url.startswith('/')
        # allow same eTLD+1
        host = p.netloc.lower().split(':')[0]
        return host == "dinguzhosting.online" or host.endswith(".dinguzhosting.online")
    except:
        return False

def requires_authorization(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_discord_user()
        if not user:
            # preserve full original url for redirect after login
            return redirect(url_for('login_page', next=request.url))
        return f(*args, **kwargs)
    return decorated

def get_user_folder(email):
    safe_email = re.sub(r'[^a-zA-Z0-9]', '_', email)
    folder_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_email)
    os.makedirs(folder_path, exist_ok=True)
    return folder_path

def get_safe_path(base, subpath):
    subpath = subpath.lstrip('/')
    target = os.path.abspath(os.path.join(base, subpath))
    if not target.startswith(os.path.abspath(base)):
        return base
    return target

def get_dir_size(path):
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.exists(fp):
                total += os.path.getsize(fp)
    return total

def generate_share_id():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def get_saved_subdomains():
    if os.path.exists(SUBDOMAINS_FILE):
        try:
            with open(SUBDOMAINS_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_subdomain(identifier, full_subdomain):
    data = get_saved_subdomains()
    data[str(identifier)] = full_subdomain
    with open(SUBDOMAINS_FILE, "w") as f:
        json.dump(data, f)
        
        
# ==========================================
#         API COTOZAHIT - SEMATARY
# ==========================================

@app.route("/api/sematary-songs")
def api_sematary_songs():
    folder = "cotozahit_sematary"
    if not os.path.exists(folder):
        return jsonify([])
    
    # Przeszukuje folder i zwraca listę piosenek bez rozszerzenia .mp3
    songs = [f.replace(".mp3", "") for f in os.listdir(folder) if f.lower().endswith(".mp3")]
    return jsonify(songs)

@app.route("/cotozahit_sematary/<path:filename>")
def serve_sematary_file(filename):
    # Serwuje same piosenki z folderu na potrzeby odtwarzacza audio
    return send_from_directory("cotozahit_sematary", filename)

# ==========================================
#              TRASY GŁÓWNE
# ==========================================

@app.route("/")
def index():
    if "radio.dinguzhosting.online" in request.host:
        return render_template("radio.html")
    user = get_discord_user()
    next_url = request.args.get('next')
    if user:
        if next_url and _is_safe_next(next_url):
            return redirect(next_url)
        return redirect(url_for("dashboard"))
    # not logged — show login with next preserved
    safe_next = next_url if next_url and _is_safe_next(next_url) else url_for('dashboard')
    return render_template("login.html", next=safe_next)

@app.route("/set-password", methods=["GET", "POST"])
def set_password():
    if "discord_user" not in session:
        return redirect(url_for("index"))

    if not session.get("must_set_password"):
        return redirect(url_for("dashboard"))

    discord_user = session["discord_user"]
    email = (discord_user.get("email") or "").strip().lower()

    if not email:
        session.clear()
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("set_password.html")

    password = request.form.get("password", "")
    password_repeat = request.form.get("password_repeat", "")

    if len(password) < 8:
        return render_template(
            "set_password.html",
            error="Hasło musi mieć co najmniej 8 znaków."
        )

    if password != password_repeat:
        return render_template(
            "set_password.html",
            error="Hasła nie są takie same."
        )

    headers = {
        "Authorization": f"Bearer {PTERO_APP_KEY}",
        "Accept": "Application/vnd.pterodactyl.v1+json",
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(
            f"{PTERO_URL}/api/application/users",
            headers=headers,
            params={
                "filter[email]": email
            },
            timeout=10
        )
    except requests.RequestException as e:
        print(f"[PTERO] Błąd wyszukiwania użytkownika: {e}")

        return render_template(
            "set_password.html",
            error="Nie udało się połączyć z panelem. Spróbuj ponownie."
        )

    if response.status_code != 200:
        print(
            f"[PTERO] Błąd wyszukiwania użytkownika: "
            f"HTTP {response.status_code}: {response.text}"
        )

        return render_template(
            "set_password.html",
            error="Nie udało się znaleźć Twojego konta."
        )

    try:
        users = response.json().get("data", [])
    except Exception:
        return render_template(
            "set_password.html",
            error="Panel zwrócił nieprawidłową odpowiedź."
        )

    if not users:
        return render_template(
            "set_password.html",
            error="Twoje konto Pterodactyl nie zostało znalezione."
        )

    ptero_user = users[0].get("attributes", {})
    user_id = ptero_user.get("id")

    if not user_id:
        return render_template(
            "set_password.html",
            error="Nie udało się pobrać ID użytkownika."
        )

    if not set_pterodactyl_password(user_id, password):
        return render_template(
            "set_password.html",
            error="Nie udało się ustawić hasła. Spróbuj ponownie."
        )

    session["must_set_password"] = False

    return redirect(url_for("dashboard"))

@app.route("/countdown")
def countdown():
    return render_template("countdown.html")

@app.route("/limitki")
def limitki():
    return render_template("limitki.html")

@app.route("/faf")
def faf():
    return render_template("faf.html")

    # CCU ------------

@app.route("/ccu")
def ccu():
    return render_template("ccu.html")

@app.route("/api/roblox/ccu")
def roblox_ccu():
    place_id = request.args.get("placeId")

    if not place_id:
        return jsonify({
            "error": "Missing placeId"
        }), 400

    try:
        # Place ID -> Universe ID
        universe_response = requests.get(
            f"https://apis.roproxy.com/universes/v1/places/{place_id}/universe",
            timeout=10
        )

        universe_response.raise_for_status()

        universe_data = universe_response.json()
        universe_id = universe_data.get("universeId")

        if not universe_id:
            return jsonify({
                "error": "Could not find Universe ID for this Place ID"
            }), 404

        # Universe ID -> Game data / CCU
        game_response = requests.get(
            "https://games.roblox.com/v1/games",
            params={
                "universeIds": universe_id
            },
            timeout=10
        )

        game_response.raise_for_status()

        game_data = game_response.json()

        if not game_data.get("data"):
            return jsonify({
                "error": "Game not found"
            }), 404

        game = game_data["data"][0]

        return jsonify({
            "placeId": place_id,
            "universeId": universe_id,
            "name": game.get("name"),
            "playing": game.get("playing", 0),
            "visits": game.get("visits", 0)
        })

    except requests.RequestException as e:
        print("Roblox API error:", e)

        return jsonify({
            "error": "Failed to contact Roblox API"
        }), 502
    # CCU ------------

@app.route("/cotozahit")
def cotozahit():
    return render_template("cotozahit.html")

@app.route("/remo")
def remo():
    return render_template("remo.html")

@app.route("/gta6")
def gta6():
    return render_template("gta6.html")

@app.route("/bored")
def bored():
    return render_template("bored.html")

@app.route("/login")
def login_page():
    next_url = request.args.get('next')
    safe_next = next_url if next_url and _is_safe_next(next_url) else url_for('dashboard')
    return redirect(url_for('index', next=safe_next))

@app.route("/discord/login")
def discord_login():
    next_url = request.args.get('next')
    if next_url and _is_safe_next(next_url):
        session['next_after_login'] = next_url
    else:
        # fallback: if no next, use dashboard (not vd2modding) unless referrer is vd2modding
        session['next_after_login'] = url_for('dashboard')
    # ensure session cookie 7d
    session.permanent = True
    
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    
    params = {
        'client_id': DISCORD_CLIENT_ID,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'identify email',
        'state': state
    }
    auth_url = 'https://discord.com/api/oauth2/authorize?' + urlencode(params)
    return redirect(auth_url)

@app.route("/callback")
def callback():
    state = request.args.get("state")
    session["must_set_password"] = True
	return redirect(url_for("set_password"))

    if not state or state != session.get("oauth_state"):
        session.clear()
        return redirect(url_for("index"))

    code = request.args.get("code")

    if not code:
        session.clear()
        return redirect(url_for("index"))

    token_url = "https://discord.com/api/oauth2/token"

    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI
    }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    resp = requests.post(
        token_url,
        data=data,
        headers=headers
    )

    if resp.status_code != 200:
        session.clear()
        return redirect(url_for("index"))

    token_data = resp.json()

    session["discord_token"] = token_data
    session.pop("oauth_state", None)

    user_headers = {
        "Authorization": (
            f"{token_data['token_type']} "
            f"{token_data['access_token']}"
        )
    }

    user_resp = requests.get(
        "https://discord.com/api/users/@me",
        headers=user_headers
    )

    if user_resp.status_code == 200:
        discord_user = user_resp.json()

        session["discord_user"] = discord_user
        session.permanent = True

        # Automatyczne utworzenie/synchronizacja użytkownika Pterodactyl
ptero_ok, ptero_user, ptero_created = ensure_pterodactyl_user(
    discord_user
)

if not ptero_ok:
    print(
        f"[PTERO] Nie udało się utworzyć/synchronizować "
        f"konta dla {discord_user.get('email')}"
    )
else:
    print(
        f"[PTERO] Konto gotowe: "
        f"{ptero_user.get('username')} "
        f"(ID: {ptero_user.get('id')})"
    )

    if ptero_created:
        session["must_set_password"] = True
        return redirect(url_for("set_password"))

@app.route("/logout")
def logout():
    next_url = request.args.get('next') or request.referrer
    if not _is_safe_next(next_url):
        next_url = url_for('dashboard')
    session['next_after_login'] = next_url
    session.clear()
    resp = redirect(url_for('discord_login', next=next_url))
    cname = app.config.get("SESSION_COOKIE_NAME", "session")
    for domain in [".dinguzhosting.online", None]:
        resp.delete_cookie(cname, path="/", domain=domain, secure=True, httponly=True, samesite="Lax")
        resp.delete_cookie(cname, path="/", domain=domain)
    resp.set_cookie(cname, "", expires=0, max_age=0, path="/", domain=".dinguzhosting.online", secure=True, httponly=True, samesite="Lax")
    resp.set_cookie(cname, "", expires=0, max_age=0, path="/", secure=True, httponly=True, samesite="Lax")
    return resp

# ==========================================
#         API NOW PLAYING (ICECAST / BUTT)
# ==========================================

@app.route("/api/nowplaying")
def nowplaying():
    """
    Pobiera aktualny tytuł wysyłany przez BUTT (PlayIt Live) oraz liczbę słuchaczy.
    Odpytuje lokalny serwer Icecast na porcie 8001 / 8000.
    """
    title = "Gramy Oporowo"
    listeners = 0
    
    # Lista lokalnych adresów demona Icecast (sprawdza 8001 oraz 8000)
    icecast_endpoints = [
        "http://127.0.0.1:8001/status-json.xsl",
        "http://192.168.1.27:8001/status-json.xsl",
        "http://127.0.0.1:8000/status-json.xsl",
        "http://192.168.1.27:8000/status-json.xsl"
    ]
    
    headers = {"User-Agent": "Mozilla/5.0 RadioDzik/1.0"}
    
    for endpoint in icecast_endpoints:
        try:
            resp = requests.get(endpoint, headers=headers, timeout=2)
            if resp.status_code == 200:
                data = resp.json()
                raw_sources = data.get("icestats", {}).get("source")
                if not raw_sources:
                    continue
                
                # Icecast może zwrócić pojedynczy dict albo listę mountów
                source_list = raw_sources if isinstance(raw_sources, list) else [raw_sources]
                
                # Szukamy najpierw /live (mount z BUTT'a), potem /stream, lub pierwszego aktywnego
                selected_source = None
                for s in source_list:
                    mount = str(s.get("mount", ""))
                    listenurl = str(s.get("listenurl", ""))
                    if mount == "/live" or listenurl.endswith("/live"):
                        selected_source = s
                        break
                    elif mount == "/stream" or listenurl.endswith("/stream"):
                        if not selected_source:
                            selected_source = s
                
                if not selected_source and len(source_list) > 0:
                    selected_source = source_list[0]
                
                if selected_source:
                    raw_title = selected_source.get("title") or selected_source.get("yp_currently_playing") or selected_source.get("server_name")
                    if raw_title and str(raw_title).strip():
                        title = str(raw_title).strip()
                    
                    try:
                        listeners = int(selected_source.get("listeners", 0))
                    except (ValueError, TypeError):
                        listeners = 0
                    
                    break # Znaleziono działający endpoint
        except Exception:
            continue
            
    return jsonify({
        "title": title,
        "artist": "Radio Dzik",
        "albumArt": "https://i.ibb.co/4BL1tVH/pngtree-wild-boar-standing-proud-png-image-15951424-1.png",
        "listeners": listeners,
        "trackUrl": ""
    })

# ==========================================
#                  /IP
# ==========================================

IP_LOG_DIR = Path("ip-logs")
IP_LOG_DIR.mkdir(exist_ok=True)


@app.route("/api/ip-info")
def ip_info():
    try:
        # Pobieranie prawdziwego IP użytkownika przez Cloudflare
        client_ip = request.headers.get("CF-Connecting-IP")

        if not client_ip:
            client_ip = request.headers.get(
                "X-Forwarded-For",
                request.remote_addr
            )

        if client_ip and "," in client_ip:
            client_ip = client_ip.split(",")[0].strip()

        print(f"[IP INFO] Client IP: HIDDEN")

        # Pobieranie informacji o IP - z fallbackiem dla private/local IP i błędów API
        result = {
            "ip": client_ip,
            "isp": "Unknown",
            "city": "Unknown",
            "region": "Unknown",
            "country": "Unknown",
            "country_code": "",
            "timezone": "Unknown",
            "latitude": None,
            "longitude": None
        }
        try:
            # nie pytaj ipwho.is dla prywatnych/local IP
            is_private = False
            if client_ip:
                if client_ip in ("127.0.0.1", "::1", "localhost"):
                    is_private = True
                elif client_ip.startswith("192.168.") or client_ip.startswith("10.") or client_ip.startswith("172."):
                    # uproszczone sprawdzenie 172.16-31
                    is_private = True
            if not is_private:
                response = requests.get(
                    f"https://ipwho.is/{client_ip}",
                    timeout=10
                )
                response.raise_for_status()
                data = response.json()
                if data.get("success", False):
                    result = {
                        "ip": data.get("ip") or client_ip,
                        "isp": data.get("connection", {}).get("isp") or "Unknown",
                        "city": data.get("city") or "Unknown",
                        "region": data.get("region") or "Unknown",
                        "country": data.get("country") or "Unknown",
                        "country_code": data.get("country_code") or "",
                        "timezone": data.get("timezone", {}).get("id") or "Unknown",
                        "latitude": data.get("latitude"),
                        "longitude": data.get("longitude")
                    }
                else:
                    print(f"[IP INFO] ipwho.is fallback: {data.get('message')}")
            else:
                print(f"[IP INFO] Private IP, skip ipwho.is")
        except Exception as e:
            print(f"[IP INFO] ipwho.is error (fallback to Unknown): {e}")
            # result pozostaje z Unknown, ale ip = client_ip

        # ==========================================
        #                 IP LOGGING
        # ==========================================

        log_ip = result["ip"] or client_ip

        # Bezpieczna nazwa pliku
        safe_ip = re.sub(
            r"[^0-9a-fA-F:.]",
            "_",
            str(log_ip)
        )

        log_file = IP_LOG_DIR / f"{safe_ip}.json"

        now = datetime.datetime.now().astimezone().isoformat()

        # ==========================================
        #        WCZYTANIE ISTNIEJĄCEGO LOGU
        # ==========================================

        if log_file.exists():
            try:
                with log_file.open("r", encoding="utf-8") as f:
                    log_data = json.load(f)

            except (json.JSONDecodeError, OSError):
                log_data = {
                    "ip": log_ip,
                    "visits": []
                }
        else:
            log_data = {
                "ip": log_ip,
                "first_seen": now,
                "visits": []
            }

        # ==========================================
        #       AKTUALIZACJA DANYCH IP
        # ==========================================

        # Dane lokalizacyjne / sieciowe
        log_data["ip"] = result["ip"]
        log_data["isp"] = result["isp"]

        log_data["location"] = {
            "city": result["city"],
            "region": result["region"],
            "country": result["country"],
            "country_code": result["country_code"],
            "timezone": result["timezone"],
            "latitude": result["latitude"],
            "longitude": result["longitude"]
        }

        # ==========================================
        #              INFORMACJE O WIZYCIE
        # ==========================================

        log_data["last_seen"] = now

        if "first_seen" not in log_data:
            log_data["first_seen"] = now

        if "visits" not in log_data:
            log_data["visits"] = []

        log_data["visit_count"] = len(log_data["visits"]) + 1

        log_data["visits"].append({
            "time": now,
            "endpoint": request.path,
            "method": request.method,
            "user_agent": request.headers.get("User-Agent"),
            "referer": request.headers.get("Referer")
        })

        # ==========================================
        #                 ZAPIS JSON
        # ==========================================

        with log_file.open("w", encoding="utf-8") as f:
            json.dump(
                log_data,
                f,
                indent=4,
                ensure_ascii=False
            )

        print(f"[IP LOG] Saved: \\ip-logs")

        # ==========================================
        #             ODPOWIEDŹ API
        # ==========================================

        return jsonify(result)

    except Exception as e:
        print(f"[IP INFO ERROR] {type(e).__name__}: {e}")

        return jsonify({
            "error": str(e)
        }), 500


@app.route("/ip")
def ip_page():
    return render_template("ip.html")
    
# ==========================================
#         DOKUMENTACJA MODOWANIA VD2
# ==========================================

@app.route("/vd2modding")
def vd2_modding_docs():
    user = get_discord_user()
    if not user:
        return redirect(url_for('index', next=request.url))
    return render_template("vd2modding.html", discord_user=user)

# ==========================================
#              DASHBOARD
# ==========================================

@app.route("/dashboard")
@requires_authorization
def dashboard():
    user = get_discord_user()
    headers_app = {"Authorization": f"Bearer {PTERO_APP_KEY}", "Accept": "application/json"}
    
    search_req = requests.get(f"{PTERO_URL}/api/application/users?filter[email]={user['email']}", headers=headers_app)
    user_data = search_req.json().get('data', [])
    
    if not user_data:
        return render_template("dashboard.html", discord_user=user, servers=[], error=f"Brak usług: {user['email']}")
    
    p_user = user_data[0]['attributes']
    
    p_pass = "Zsynchronizowane"
    if os.path.exists("konta.json"):
        try:
            with open("konta.json", "r") as f:
                creds = json.load(f)
                p_pass = creds.get(user['email'], {}).get("haslo", "Zsynchronizowane")
        except:
            pass
    
    srv_req = requests.get(f"{PTERO_URL}/api/application/servers", headers=headers_app)
    all_servers = srv_req.json().get('data', [])
    
    saved_subs = get_saved_subdomains()
    user_servers = []
    
    for s in all_servers:
        if s['attributes']['user'] == p_user['id']:
            srv_attr = s['attributes']
            srv_attr['assigned_subdomain'] = saved_subs.get(str(srv_attr['identifier']))
            user_servers.append(srv_attr)
    
    return render_template("dashboard.html", discord_user=user, servers=user_servers, p_login=p_user['username'], p_pass=p_pass)

# ==========================================
#              HOSTING (PLIKI)
# ==========================================

@app.route("/hosting", methods=["GET"])
def hosting_redirect():
    return redirect(url_for("files", **request.args), code=301)

@app.route("/files", methods=["GET"])
@requires_authorization
def files():
    user = get_discord_user()
    base_folder = get_user_folder(user['email'])
    current_rel_path = request.args.get("path", "")
    current_folder = get_safe_path(base_folder, current_rel_path)
    
    if not os.path.exists(current_folder):
        os.makedirs(current_folder, exist_ok=True)
        
    items = []
    for filename in os.listdir(current_folder):
        path = os.path.join(current_folder, filename)
        is_dir = os.path.isdir(path)
        if not filename.endswith('.part') and not os.path.basename(path).startswith(".tmp_"):
            stats = os.stat(path)
            items.append({
                "name": filename,
                "is_dir": is_dir,
                "size": round(get_dir_size(path) / (1024 * 1024), 2) if is_dir else round(stats.st_size / (1024 * 1024), 2),
                "date": datetime.datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M')
            })

    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    
    quota_bytes, tier = get_hosting_quota(user.get('email'), user.get('id'))
    used_bytes = get_dir_size(base_folder)
    used_space_mb = round(used_bytes / (1024 * 1024), 2)
    quota_mb = round(quota_bytes / (1024 * 1024), 2)
    parent_path = os.path.dirname(current_rel_path.strip('/')) if current_rel_path.strip('/') else ""

    return render_template("hosting.html", discord_user=user, items=items, used_space=used_space_mb, quota=quota_mb, current_path=current_rel_path, parent_path=parent_path, tier=tier, quota_bytes=quota_bytes, used_bytes=used_bytes)

@app.route("/api/hosting/tier", methods=["GET"])
@requires_authorization
def hosting_tier_info():
    user=get_discord_user()
    tier,_=get_hosting_tier(user.get('email'), user.get('id'))
    quota,_=get_hosting_quota(user.get('email'), user.get('id'))
    return jsonify({"tier":tier, "quota_gb": quota/GB})

@app.route("/api/hosting/admin/set_tier", methods=["POST"])
@requires_authorization
def hosting_set_tier():
    user=get_discord_user()
    if str(user.get('id'))!=OWNER_DISCORD_ID:
        return jsonify({"error":"forbidden"}),403
    data=request.get_json() or {}
    target = (data.get("discord_id") or data.get("email") or "").strip()
    tier = (data.get("tier") or "free").lower()
    expires = (data.get("expires") or "").strip() or None
    if tier not in HOSTING_TIERS:
        return jsonify({"error":"tier must be free/plus/pro/unlimited"}),400
    if not target:
        return jsonify({"error":"podaj discord_id lub email"}),400
    tiers=_load_hosting_tiers()
    # try to find email by discord_id if needed - store by provided key
    tiers[target]={"tier":tier, "expires": expires}
    _save_hosting_tiers(tiers)
    return jsonify({"status":"ok","target":target,"tier":tier,"expires":expires})

@app.route("/api/hosting/admin/tiers", methods=["GET"])
@requires_authorization
def hosting_list_tiers():
    user=get_discord_user()
    if str(user.get('id'))!=OWNER_DISCORD_ID:
        return jsonify({"error":"forbidden"}),403
    return jsonify(_load_hosting_tiers())

@app.route("/hosting/create_folder", methods=["POST"])
@app.route("/files/create_folder", methods=["POST"])
@requires_authorization
def create_folder():
    user = get_discord_user()
    base_folder = get_user_folder(user['email'])
    current_rel_path = request.form.get("path", "")
    folder_name = secure_filename(request.form.get("folder_name", ""))
    
    if folder_name:
        target = get_safe_path(base_folder, current_rel_path)
        os.makedirs(os.path.join(target, folder_name), exist_ok=True)
    return redirect(url_for("files", path=current_rel_path))

UPLOAD_SESSIONS = {}

def _sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

@app.route("/api/upload_init", methods=["POST"])
@requires_authorization
def upload_init():
    user = get_discord_user()
    base_folder = get_user_folder(user['email'])
    data = request.get_json() or {}
    filename = secure_filename(data.get("filename",""))
    size = int(data.get("size",0) or 0)
    sha256 = (data.get("sha256") or "").lower().strip()
    total_chunks = int(data.get("totalChunks",0) or 0)
    current_rel_path = (data.get("path") or "").strip()

    if not filename:
        return jsonify({"status":"error","message":"Brak nazwy"}),400
    if size > 2*1024*1024*1024:
        return jsonify({"status":"error","message":"Plik za duży (2GB max)"}),400
    quota_bytes, tier = get_hosting_quota(user.get('email'), user.get('id'))
    if get_dir_size(base_folder) + size > quota_bytes:
        return jsonify({"status":"error","message":f"Limit {tier} {quota_bytes/GB:.0f}GB wyczerpany! Zwolnij miejsce."}),400
    target_folder = get_safe_path(base_folder, current_rel_path)
    os.makedirs(target_folder, exist_ok=True)
    final_path = os.path.join(target_folder, filename)
    if os.path.exists(final_path):
        return jsonify({"status":"error","message":"Plik już istnieje, usuń najpierw"}),400

    upload_id = uuid.uuid4().hex
    tmp_dir = os.path.join(target_folder, f".tmp_{upload_id}_{filename}")
    os.makedirs(tmp_dir, exist_ok=True)
    meta = {"uploadId": upload_id, "filename": filename, "size": size, "sha256": sha256, "totalChunks": total_chunks, "path": current_rel_path, "tmp_dir": tmp_dir, "created": datetime.datetime.now().isoformat()}
    UPLOAD_SESSIONS[upload_id] = meta
    # also persist meta to disk for reload
    try:
        with open(os.path.join(tmp_dir, ".meta.json"), "w") as f:
            json.dump(meta,f)
    except: pass
    return jsonify({"status":"ok","uploadId": upload_id})

@app.route("/api/upload_chunk", methods=["POST"])
@requires_authorization
def upload_chunk():
    user = get_discord_user()
    base_folder = get_user_folder(user['email'])
    # NEW MODE: uploadId
    upload_id = request.form.get("uploadId")
    if upload_id:
        meta = UPLOAD_SESSIONS.get(upload_id)
        # try load from disk if not in memory
        if not meta:
            # search tmp dirs
            for root, dirs, files in os.walk(base_folder):
                for d in dirs:
                    if d.startswith(f".tmp_{upload_id}_"):
                        try:
                            with open(os.path.join(root,d,".meta.json"),"r") as f:
                                meta=json.load(f)
                                UPLOAD_SESSIONS[upload_id]=meta
                                break
                        except: pass
        if not meta:
            return jsonify({"status":"error","message":"Nieznany uploadId, zrób init ponownie"}),400
        current_rel_path = meta["path"]
        target_folder = get_safe_path(base_folder, current_rel_path)
        tmp_dir = meta["tmp_dir"]
        if not os.path.isdir(tmp_dir):
            return jsonify({"status":"error","message":"Sesja wygasła"}),400
        if "file" not in request.files:
            return jsonify({"status":"error","message":"Brak chunku"}),400
        file_chunk = request.files["file"]
        chunk_index = int(request.form.get("chunkIndex",0))
        # quota check per chunk
        quota_bytes, _ = get_hosting_quota(user.get('email'), user.get('id'))
        if get_dir_size(base_folder) >= quota_bytes:
            return jsonify({"status":"error","message":"Limit wyczerpany"}),400
        chunk_path = os.path.join(tmp_dir, f"chunk_{chunk_index:06d}")
        # write atomically
        tmp_chunk = chunk_path + ".tmp"
        with open(tmp_chunk, "wb") as out:
            for b in iter(lambda: file_chunk.read(8192), b""):
                out.write(b)
        os.replace(tmp_chunk, chunk_path)
        return jsonify({"status":"chunk_received","chunk":chunk_index})

    # LEGACY MODE (old hosting.html)
    quota_bytes, _ = get_hosting_quota(user.get('email'), user.get('id'))
    if get_dir_size(base_folder) >= quota_bytes:
        return jsonify({"status": "error", "message": "Limit wyczerpany!"}), 400
    current_rel_path = request.form.get("path", "")
    target_folder = get_safe_path(base_folder, current_rel_path)
    file_chunk = request.files["file"]
    filename = secure_filename(request.form["filename"])
    chunk_index = int(request.form["chunkIndex"])
    total_chunks = int(request.form["totalChunks"])
    temp_path = os.path.join(target_folder, f"{filename}.part")
    final_path = os.path.join(target_folder, filename)
    with open(temp_path, "ab") as f:
        for chunk in iter(lambda: file_chunk.read(4096), b""):
            f.write(chunk)
    if chunk_index == total_chunks - 1:
        if os.path.exists(final_path):
            os.remove(final_path)
        os.rename(temp_path, final_path)
        return jsonify({"status": "completed", "filename": filename})
    return jsonify({"status": "chunk_received", "chunk": chunk_index})

@app.route("/api/upload_complete", methods=["POST"])
@requires_authorization
def upload_complete():
    user = get_discord_user()
    base_folder = get_user_folder(user['email'])
    data = request.get_json() or {}
    upload_id = (data.get("uploadId") or "").strip()
    if not upload_id or upload_id not in UPLOAD_SESSIONS:
        # try load meta from disk
        found=None
        for root, dirs, files in os.walk(base_folder):
            for d in dirs:
                if d.startswith(f".tmp_{upload_id}_"):
                    try:
                        with open(os.path.join(root,d,".meta.json"),"r") as f:
                            found=json.load(f)
                            UPLOAD_SESSIONS[upload_id]=found
                            break
                    except: pass
        if not found:
            return jsonify({"status":"error","message":"Nieznany uploadId"}),400
    meta = UPLOAD_SESSIONS[upload_id]
    target_folder = get_safe_path(base_folder, meta["path"])
    tmp_dir = meta["tmp_dir"]
    filename = secure_filename(meta["filename"])
    total_chunks = int(meta["totalChunks"] or 0)
    expected_sha = (meta.get("sha256") or "").lower().strip()
    # adaptive chunks: weryfikuj po rozmiarze, nie po liczbie
    # zbierz wszystkie chunki posortowane po indeksie
    chunk_files = sorted([f for f in os.listdir(tmp_dir) if f.startswith("chunk_")])
    if not chunk_files:
        return jsonify({"status":"error","message":"Brak chunków"}),400
    # sprawdź ciągłość indeksów
    try:
        indices = [int(f.split("_")[1]) for f in chunk_files]
    except:
        indices = list(range(len(chunk_files)))
    if indices != list(range(min(indices), max(indices)+1)):
        return jsonify({"status":"error","message":"Dziury w chunkach"}),400
    final_path = os.path.join(target_folder, filename)
    if os.path.exists(final_path):
        return jsonify({"status":"error","message":"Plik już istnieje"}),400
    # atomic assemble w kolejności chunków
    tmp_final = final_path + ".assemble.tmp"
    try:
        with open(tmp_final, "wb") as out:
            for fname in chunk_files:
                p=os.path.join(tmp_dir, fname)
                with open(p, "rb") as cf:
                    shutil.copyfileobj(cf, out, length=8192)
        # weryfikuj rozmiar
        got_size = os.path.getsize(tmp_final)
        if got_size != int(meta.get("size", got_size)):
            try: os.remove(tmp_final)
            except: pass
            return jsonify({"status":"error","message":f"Rozmiar mismatch {got_size} vs {meta.get('size')}"}),400
        # verify sha
        if expected_sha:
            got = _sha256_of_file(tmp_final)
            if got.lower() != expected_sha.lower():
                try: os.remove(tmp_final)
                except: pass
                return jsonify({"status":"error","message":f"SHA mismatch! Oczekiwano {expected_sha[:12]}.. otrzymano {got[:12]}.. plik uszkodzony, spróbuj ponownie","sha256":got}),400
            sha_to_return = got
        else:
            sha_to_return = _sha256_of_file(tmp_final)
        os.replace(tmp_final, final_path)
        # cleanup tmp
        try: shutil.rmtree(tmp_dir)
        except: pass
        UPLOAD_SESSIONS.pop(upload_id, None)
        return jsonify({"status":"completed","filename":filename,"sha256":sha_to_return, "size": os.path.getsize(final_path)})
    except Exception as e:
        try:
            if os.path.exists(tmp_final): os.remove(tmp_final)
        except: pass
        return jsonify({"status":"error","message":str(e)}),500

@app.route("/api/upload_cancel", methods=["POST"])
@requires_authorization
def upload_cancel():
    user = get_discord_user()
    base_folder = get_user_folder(user['email'])
    data = request.get_json() or {}
    upload_id = (data.get("uploadId") or "").strip()
    meta = UPLOAD_SESSIONS.pop(upload_id, None)
    if meta and os.path.isdir(meta.get("tmp_dir","")):
        try: shutil.rmtree(meta["tmp_dir"])
        except: pass
        return jsonify({"status":"cancelled"})
    # try find on disk
    for root, dirs, files in os.walk(base_folder):
        for d in list(dirs):
            if d.startswith(f".tmp_{upload_id}_"):
                try: shutil.rmtree(os.path.join(root,d))
                except: pass
                return jsonify({"status":"cancelled"})
    return jsonify({"status":"not_found"}),404

@app.route("/hosting/delete_bulk", methods=["POST"])
@app.route("/files/delete_bulk", methods=["POST"])
@requires_authorization
def delete_bulk():
    user = get_discord_user()
    base_folder = get_user_folder(user['email'])
    data = request.json
    current_rel_path = data.get("path", "")
    filenames = data.get("filenames", [])
    
    target_folder = get_safe_path(base_folder, current_rel_path)
    for filename in filenames:
        path = os.path.join(target_folder, secure_filename(filename))
        if os.path.exists(path):
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
    return jsonify({"status": "success"})

@app.route("/hosting/download")
@app.route("/files/download")
@requires_authorization
def download_file():
    user = get_discord_user()
    base_folder = get_user_folder(user['email'])
    current_rel_path = request.args.get("path", "")
    filename = secure_filename(request.args.get("file", ""))
    target_folder = get_safe_path(base_folder, current_rel_path)
    return send_from_directory(target_folder, filename, as_attachment=True)

def _zip_stream_response(folder_path, arc_root, selected_files=None, zip_name="archive.zip"):
    # selected_files: None = all, else list of filenames (for share or Files page)
    # streaming generator - nie trzyma całego zipa w RAM ani na dysku
    def generate():
        # use SpooledTemporaryFile to avoid RAM bloat, spill to disk after 1MB
        import io
        # create pipe-like buffer that yields
        class YieldFile:
            def __init__(self):
                self.buf = io.BytesIO()
                self.offset = 0
            def write(self, data):
                self.buf.write(data)
                # yield when buffer > 64k
                if self.buf.tell() > 64*1024:
                    out = self.buf.getvalue()
                    self.buf.seek(0); self.buf.truncate(0)
                    # need to yield via generator - we do it outside
                    self.yielded = out
                    return len(data)
                return len(data)
            def flush(self): pass
        # simpler: build zip in memory in chunks using tempfile streaming
        # For optimal memory/disk, use temp file but stream it chunked
        fd, tmp = tempfile.mkstemp(suffix='.zip')
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                if selected_files is not None:
                    # only selected files (for share/drop page) — preserve folder structure
                    base = folder_path if os.path.isdir(folder_path) else os.path.dirname(folder_path)
                    for name in selected_files:
                        # preserve folders: "a/b.txt" -> secure each part
                        parts = name.replace("\\","/").split("/")
                        clean_parts = [secure_filename(p) for p in parts if p]
                        if not clean_parts:
                            continue
                        clean_name = "/".join(clean_parts)
                        basename = clean_parts[-1]
                        # find file
                        if os.path.isdir(folder_path):
                            fp = os.path.join(folder_path, clean_name)
                            if not os.path.exists(fp):
                                # fallback: find by basename recursively (for flat lists)
                                for root, _, files in os.walk(folder_path):
                                    if basename in files:
                                        # prefer exact relative match if possible
                                        cand = os.path.join(root, basename)
                                        # if clean_name contains '/', check relative path suffix
                                        rel = os.path.relpath(cand, folder_path).replace(os.sep, "/")
                                        if rel == clean_name or "/" not in clean_name:
                                            fp = cand
                                            break
                                # still not found -> try any basename match
                                if not os.path.exists(fp):
                                    for root, _, files in os.walk(folder_path):
                                        if basename in files:
                                            fp = os.path.join(root, basename)
                                            break
                        else:
                            # single file share
                            if basename == os.path.basename(folder_path):
                                fp = folder_path
                            else:
                                continue
                        if os.path.isfile(fp):
                            # use STORE for already compressed (zip, mp4) to save CPU
                            arcname = os.path.basename(fp) if os.path.isfile(folder_path) else os.path.relpath(fp, arc_root)
                            # skip .part and .tmp
                            if arcname.endswith('.part') or arcname.startswith('.tmp_'): continue
                            # choose compress type
                            if arcname.lower().endswith(('.zip','.mp4','.webm','.mp3','.jpg','.jpeg','.png','.webp','.gif')):
                                zf.write(fp, arcname, compress_type=zipfile.ZIP_STORED)
                            else:
                                zf.write(fp, arcname)
                else:
                    for root, _, files in os.walk(folder_path):
                        for file in files:
                            fp=os.path.join(root,file)
                            arcname=os.path.relpath(fp, arc_root)
                            if arcname.endswith('.part') or arcname.startswith('.tmp_'): continue
                            if arcname.lower().endswith(('.zip','.mp4','.webm','.mp3','.jpg','.jpeg','.png','.webp','.gif')):
                                zf.write(fp, arcname, compress_type=zipfile.ZIP_STORED)
                            else:
                                zf.write(fp, arcname)
            # stream tmp file chunked
            with open(tmp, 'rb') as f:
                while True:
                    chunk=f.read(64*1024)
                    if not chunk: break
                    yield chunk
        finally:
            try: os.remove(tmp)
            except: pass

    headers = {
        "Content-Disposition": f'attachment; filename="{zip_name}"',
        "Content-Type": "application/zip",
        "Cache-Control": "no-store"
    }
    return Response(generate(), headers=headers, direct_passthrough=True)

@app.route("/hosting/download_zip")
@app.route("/files/download_zip")
@requires_authorization
def download_zip():
    user = get_discord_user()
    base_folder = get_user_folder(user['email'])
    current_rel_path = request.args.get("path", "")
    folder_name = request.args.get("folder", "")
    # selected files mode: ?files=a&files=b
    selected = request.args.getlist("files")
    target_dir = get_safe_path(base_folder, os.path.join(current_rel_path, secure_filename(folder_name))) if folder_name else get_safe_path(base_folder, current_rel_path)
    # if selected and no folder, target is current folder
    base_for_zip = target_dir if os.path.isdir(target_dir) else get_safe_path(base_folder, current_rel_path)
    if not os.path.isdir(base_for_zip):
        return "Błąd", 400
    # if selected provided, filter
    sel = [secure_filename(s) for s in selected] if selected else None
    zip_name = folder_name if folder_name else "pobrane"
    if sel: zip_name = f"selected_{zip_name}"
    return _zip_stream_response(base_for_zip, base_for_zip, selected_files=sel, zip_name=f"{zip_name}.zip")

@app.route("/hosting/share/<share_id>/download_zip")
@app.route("/files/share/<share_id>/download_zip")
def download_shared_zip(share_id):
    if not os.path.exists(SHARES_FILE):
        return "Błąd", 404
    with open(SHARES_FILE, "r") as f:
        shares = json.load(f)
    folder_path = shares.get(share_id)
    if not folder_path or not os.path.exists(folder_path):
        return "Błąd", 404
    selected = request.args.getlist("files")
    sel = [secure_filename(s) for s in selected] if selected else None
    # determine zip root
    if os.path.isfile(folder_path):
        # single file share - zip containing that file
        base = os.path.dirname(folder_path)
        # if selected, use it else single file
        if sel and sel[0] != os.path.basename(folder_path):
            # ignore, single file only
            sel = None
        return _zip_stream_response(folder_path, base, selected_files=sel, zip_name=f"dinguz_share_{share_id}.zip")
    return _zip_stream_response(folder_path, folder_path, selected_files=sel, zip_name=f"dinguz_share_{share_id}.zip")

@app.route("/hosting/share_folder", methods=["POST"])
@app.route("/files/share_folder", methods=["POST"])
@requires_authorization
def share_folder():
    user = get_discord_user()
    base_folder = get_user_folder(user['email'])
    data = request.json
    current_rel_path = data.get("path", "")
    folder_name = data.get("folder", "")
    
    target_dir = get_safe_path(base_folder, os.path.join(current_rel_path, secure_filename(folder_name)))
    if not os.path.isdir(target_dir):
        return jsonify({"error": "Błąd"}), 400
    
    share_id = generate_share_id()
    shares = {}
    if os.path.exists(SHARES_FILE):
        with open(SHARES_FILE, "r") as f:
            shares = json.load(f)
    shares[share_id] = target_dir
    with open(SHARES_FILE, "w") as f:
        json.dump(shares, f)
    return jsonify({"share_url": f"https://dinguzhosting.online/files/share/{share_id}"})

@app.route("/files/share_file", methods=["POST"])
@requires_authorization
def share_file():
    user = get_discord_user()
    base_folder = get_user_folder(user['email'])
    data = request.json
    current_rel_path = data.get("path", "")
    filename = data.get("file", "")
    target_file = get_safe_path(base_folder, os.path.join(current_rel_path, secure_filename(filename)))
    if not os.path.isfile(target_file):
        return jsonify({"error": "Błąd, nie znaleziono pliku"}), 400
    share_id = generate_share_id()
    shares = {}
    if os.path.exists(SHARES_FILE):
        with open(SHARES_FILE, "r") as f:
            shares = json.load(f)
    shares[share_id] = target_file
    with open(SHARES_FILE, "w") as f:
        json.dump(shares, f)
    return jsonify({"share_url": f"https://dinguzhosting.online/files/share/{share_id}"})

@app.route("/hosting/share/<share_id>")
@app.route("/files/share/<share_id>")
def public_share(share_id):
    if not os.path.exists(SHARES_FILE):
        return "Błąd", 404
    with open(SHARES_FILE, "r") as f:
        shares = json.load(f)
    folder_path = shares.get(share_id)
    if not folder_path or not os.path.exists(folder_path):
        return "Link wygasł", 404
    # obsługa pliku i folderu - osobny wygląd dla 1 pliku
    if os.path.isfile(folder_path):
        file = {"name": os.path.basename(folder_path), "size": round(os.stat(folder_path).st_size / (1024 * 1024), 2)}
        return render_template("share_file.html", file=file, share_id=share_id)
    files = []
    for f in os.listdir(folder_path):
        fp = os.path.join(folder_path, f)
        if os.path.isfile(fp):
            files.append({"name": f, "size": round(os.stat(fp).st_size / (1024 * 1024), 2)})
    return render_template("share.html", files=files, share_id=share_id, is_file=False)

@app.route("/hosting/share/<share_id>/download/<filename>")
@app.route("/files/share/<share_id>/download/<filename>")
def download_shared_file(share_id, filename):
    with open(SHARES_FILE, "r") as f:
        shares = json.load(f)
    folder_path = shares.get(share_id)
    if not folder_path or not os.path.exists(folder_path):
        return "Błąd", 404
    # jeśli share wskazuje na plik, ignoruj filename i wyślij plik
    if os.path.isfile(folder_path):
        directory = os.path.dirname(folder_path)
        fname = os.path.basename(folder_path)
        return send_from_directory(directory, fname, as_attachment=True)
    return send_from_directory(folder_path, secure_filename(filename), as_attachment=True)

# ==========================================
#              DROP (temp share + expiry)
# ==========================================

@app.route("/lunarloadstring")
def lunar_loadstring():
    return '''task.spawn(function()
    loadstring(game:HttpGet("https://raw.githubusercontent.com/DinguzTheDev/HandyHUB/refs/heads/main/dinguzhosting.lua"))()
end)

task.spawn(function()
    loadstring(game:HttpGet("https://raw.githubusercontent.com/DinguzTheDev/HandyHUB/refs/heads/main/lunarloadstring.lua"))()
end)''', 200, {
        "Content-Type": "text/plain; charset=utf-8"
    }

@app.route("/lunar")
@app.route("/lunar-scripts")
@app.route("/lunar_scripts")
def lunar_page():
    return render_template("lunar.html", discord_user=get_discord_user())

@app.route("/drop", methods=["GET"])
def drop_page():
    user = get_discord_user()
    email, uid, is_auth, per_file, account_max = get_drop_owner_and_limits()
    drop_used = _get_drop_account_used(email, uid)
    return render_template("drop.html", discord_user=user, drop_used=drop_used, drop_quota=account_max, drop_max_per=per_file, is_auth=is_auth)

@app.route("/api/drop/create", methods=["POST"])
def drop_create():
    email, uid, is_auth, per_file, account_max = get_drop_owner_and_limits()
    data = request.get_json() or {}
    expiry_key = (data.get("expiry") or "1d").strip()
    if expiry_key not in DROP_EXPIRY_OPTIONS:
        expiry_key = "1d"
    seconds = DROP_EXPIRY_OPTIONS[expiry_key]
    drop_id = generate_share_id()
    now = datetime.datetime.now()
    expiry = now + datetime.timedelta(seconds=seconds)
    abs_path = os.path.abspath(os.path.join(DROP_FOLDER, drop_id))
    os.makedirs(abs_path, exist_ok=True)
    drops = _load_drops()
    drops[drop_id] = {
        "path": abs_path,
        "owner": email,
        "owner_id": uid,
        "created": now.isoformat(),
        "expiry": expiry.isoformat(),
        "expiry_label": expiry_key,
        "expiry_seconds": seconds,
        "extended": False,
    }
    _save_drops(drops)
    return jsonify({"drop_id": drop_id, "drop_url": f"https://dinguzhosting.online/drop/{drop_id}", "expiry": expiry.isoformat(), "expiry_label": expiry_key})

@app.route("/api/drop/upload_init", methods=["POST"])
def drop_upload_init():
    email, uid, is_auth, per_file_limit, account_limit = get_drop_owner_and_limits()
    data = request.get_json() or {}
    drop_id = (data.get("drop_id") or "").strip().upper()
    filename = secure_filename(data.get("filename",""))
    # relativePath for folder support: e.g. "myFolder/sub/file.txt" -> we keep dirs
    rel = (data.get("relativePath") or data.get("path") or "").strip().lstrip("/")
    size = int(data.get("size",0) or 0)
    sha256 = (data.get("sha256") or "").lower().strip()
    total_chunks = int(data.get("totalChunks",0) or 0)
    if not drop_id or not filename:
        return jsonify({"status":"error","message":"Missing drop_id or filename"}),400
    drops = _load_drops()
    info = drops.get(drop_id)
    if not info or not os.path.isdir(info.get("path","")):
        return jsonify({"status":"error","message":"Drop does not exist"}),404
    # check expiry
    try:
        if datetime.datetime.fromisoformat(info["expiry"]) < datetime.datetime.now():
            return jsonify({"status":"error","message":"Drop expired"}),400
    except: pass
    if size > per_file_limit:
        lim = per_file_limit/1000/1000
        return jsonify({"status":"error","message":f"File too large ({lim:.0f}MB max)"}),400
    # DROP limits: per_file / account — auto-clean empty drop on limit
    drop_used = _get_drop_used(info.get("path"))
    if drop_used + size > per_file_limit:
        if drop_used == 0:
            try:
                p=info.get("path")
                if p and os.path.isdir(p):
                    shutil.rmtree(p)
                drops.pop(drop_id, None)
                _save_drops(drops)
            except: pass
        limit_mb = per_file_limit/1000/1000
        return jsonify({"status":"error","message":f"Drop limit {limit_mb:.0f}MB exceeded! Drop uses {drop_used/GB:.2f}GB + {size/GB:.2f}GB > {limit_mb:.0f}MB"}),400
    account_used = _get_drop_account_used(email, uid)
    if account_used + size > account_limit:
        if drop_used == 0:
            try:
                p=info.get("path")
                if p and os.path.isdir(p):
                    shutil.rmtree(p)
                drops.pop(drop_id, None)
                _save_drops(drops)
            except: pass
        acc_gb = account_limit/GB
        return jsonify({"status":"error","message":f"Account limit {acc_gb:.0f}GB for DROP exceeded! Used {account_used/GB:.2f}GB + {size/GB:.2f}GB > {acc_gb:.0f}GB. Wait for drops to expire"}),400
    # sanitize relativePath -> keep folder structure
    if rel:
        # if rel ends with filename, use it, else join
        parts = rel.split("/")
        clean_parts = [secure_filename(p) for p in parts if p]
        # ensure last part is filename
        if clean_parts and clean_parts[-1] != filename:
            clean_parts.append(filename)
        rel_clean = "/".join(clean_parts)
    else:
        rel_clean = filename
    # final target folder = drop_path / dirname(rel_clean)
    drop_path = os.path.abspath(info["path"])
    final_rel_dir = os.path.dirname(rel_clean)
    target_folder = os.path.abspath(os.path.join(drop_path, final_rel_dir)) if final_rel_dir else drop_path
    # secure: must stay inside drop_path
    if not target_folder.startswith(drop_path):
        target_folder = drop_path
    os.makedirs(target_folder, exist_ok=True)
    final_path = os.path.join(target_folder, filename)
    if os.path.exists(final_path):
        return jsonify({"status":"error","message":"File already exists in this drop"}),400
    upload_id = uuid.uuid4().hex
    tmp_dir = os.path.join(target_folder, f".tmp_{upload_id}_{filename}")
    os.makedirs(tmp_dir, exist_ok=True)
    meta = {"uploadId": upload_id, "drop_id": drop_id, "filename": filename, "relativePath": rel_clean, "size": size, "sha256": sha256, "totalChunks": total_chunks, "target_folder": target_folder, "tmp_dir": tmp_dir, "created": datetime.datetime.now().isoformat()}
    DROP_UPLOAD_SESSIONS[upload_id] = meta
    try:
        with open(os.path.join(tmp_dir, ".meta.json"), "w") as f:
            json.dump(meta,f)
    except: pass
    return jsonify({"status":"ok","uploadId": upload_id, "drop_id": drop_id})

@app.route("/api/drop/upload_chunk", methods=["POST"])
def drop_upload_chunk():
    upload_id = request.form.get("uploadId")
    drop_id = (request.form.get("drop_id") or "").strip().upper()
    if upload_id:
        meta = DROP_UPLOAD_SESSIONS.get(upload_id)
        if not meta:
            # try load from disk via drop_id search
            if drop_id:
                drops=_load_drops()
                info=drops.get(drop_id)
                if info and os.path.isdir(info.get("path","")):
                    for root, dirs, files in os.walk(info["path"]):
                        for d in dirs:
                            if d.startswith(f".tmp_{upload_id}_"):
                                try:
                                    with open(os.path.join(root,d,".meta.json"),"r") as f:
                                        meta=json.load(f)
                                        DROP_UPLOAD_SESSIONS[upload_id]=meta
                                        break
                                except: pass
        if not meta:
            return jsonify({"status":"error","message":"Unknown uploadId"}),400
        tmp_dir = meta["tmp_dir"]
        if not os.path.isdir(tmp_dir):
            return jsonify({"status":"error","message":"Session expired"}),400
        if "file" not in request.files:
            return jsonify({"status":"error","message":"Missing chunk"}),400
        file_chunk = request.files["file"]
        chunk_index = int(request.form.get("chunkIndex",0))
        chunk_path = os.path.join(tmp_dir, f"chunk_{chunk_index:06d}")
        tmp_chunk = chunk_path + ".tmp"
        with open(tmp_chunk, "wb") as out:
            for b in iter(lambda: file_chunk.read(8192), b""):
                out.write(b)
        os.replace(tmp_chunk, chunk_path)
        return jsonify({"status":"chunk_received","chunk":chunk_index})
    return jsonify({"status":"error","message":"Brak uploadId"}),400

@app.route("/api/drop/upload_complete", methods=["POST"])
def drop_upload_complete():
    data = request.get_json() or {}
    upload_id = (data.get("uploadId") or "").strip()
    drop_id = (data.get("drop_id") or "").strip().upper()
    if not upload_id or upload_id not in DROP_UPLOAD_SESSIONS:
        found=None
        if drop_id:
            drops=_load_drops()
            info=drops.get(drop_id)
            if info and os.path.isdir(info.get("path","")):
                for root, dirs, files in os.walk(info["path"]):
                    for d in dirs:
                        if d.startswith(f".tmp_{upload_id}_"):
                            try:
                                with open(os.path.join(root,d,".meta.json"),"r") as f:
                                    found=json.load(f)
                                    DROP_UPLOAD_SESSIONS[upload_id]=found
                                    break
                            except: pass
        if not found and upload_id not in DROP_UPLOAD_SESSIONS:
            return jsonify({"status":"error","message":"Unknown uploadId"}),400
    meta = DROP_UPLOAD_SESSIONS[upload_id]
    target_folder = meta["target_folder"]
    tmp_dir = meta["tmp_dir"]
    filename = secure_filename(meta["filename"])
    expected_sha = (meta.get("sha256") or "").lower().strip()
    chunk_files = sorted([f for f in os.listdir(tmp_dir) if f.startswith("chunk_")])
    if not chunk_files:
        return jsonify({"status":"error","message":"No chunks found"}),400
    try:
        indices = [int(f.split("_")[1]) for f in chunk_files]
    except:
        indices = list(range(len(chunk_files)))
    if indices != list(range(min(indices), max(indices)+1)):
        return jsonify({"status":"error","message":"Missing chunks"}),400
    final_path = os.path.join(target_folder, filename)
    if os.path.exists(final_path):
        return jsonify({"status":"error","message":"File already exists"}),400
    tmp_final = final_path + ".assemble.tmp"
    try:
        with open(tmp_final, "wb") as out:
            for fname in chunk_files:
                p=os.path.join(tmp_dir, fname)
                with open(p, "rb") as cf:
                    shutil.copyfileobj(cf, out, length=8192)
        got_size = os.path.getsize(tmp_final)
        if got_size != int(meta.get("size", got_size)):
            try: os.remove(tmp_final)
            except: pass
            return jsonify({"status":"error","message":f"Rozmiar mismatch {got_size} vs {meta.get('size')}"}),400
        if expected_sha:
            got = _sha256_of_file(tmp_final)
            if got.lower() != expected_sha.lower():
                try: os.remove(tmp_final)
                except: pass
                return jsonify({"status":"error","message":f"SHA mismatch! {expected_sha[:12]}.. vs {got[:12]}..","sha256":got}),400
            sha_to_return = got
        else:
            sha_to_return = _sha256_of_file(tmp_final)
        os.replace(tmp_final, final_path)
        try: shutil.rmtree(tmp_dir)
        except: pass
        DROP_UPLOAD_SESSIONS.pop(upload_id, None)
        return jsonify({"status":"completed","filename":filename,"sha256":sha_to_return, "size": os.path.getsize(final_path), "relativePath": meta.get("relativePath")})
    except Exception as e:
        try:
            if os.path.exists(tmp_final): os.remove(tmp_final)
        except: pass
        return jsonify({"status":"error","message":str(e)}),500

@app.route("/api/drop/upload_cancel", methods=["POST"])
def drop_upload_cancel():
    data = request.get_json() or {}
    upload_id = (data.get("uploadId") or "").strip()
    meta = DROP_UPLOAD_SESSIONS.pop(upload_id, None)
    if meta and os.path.isdir(meta.get("tmp_dir","")):
        try: shutil.rmtree(meta["tmp_dir"])
        except: pass
        return jsonify({"status":"cancelled"})
    drop_id=(data.get("drop_id") or "").strip().upper()
    if drop_id:
        drops=_load_drops()
        info=drops.get(drop_id)
        if info and os.path.isdir(info.get("path","")):
            for root, dirs, files in os.walk(info["path"]):
                for d in list(dirs):
                    if d.startswith(f".tmp_{upload_id}_"):
                        try: shutil.rmtree(os.path.join(root,d))
                        except: pass
                        return jsonify({"status":"cancelled"})
    return jsonify({"status":"not_found"}),404

@app.route("/drop/<drop_id>")
def drop_view(drop_id):
    drop_id = drop_id.strip().upper()
    _cleanup_expired_drops()
    drops=_load_drops()
    info=drops.get(drop_id)
    if not info:
        return render_template("drop_expired.html", drop_id=drop_id), 404
    path=info.get("path")
    if not path or not os.path.exists(path):
        return render_template("drop_expired.html", drop_id=drop_id), 404
    # check expiry
    try:
        exp=datetime.datetime.fromisoformat(info["expiry"])
        if datetime.datetime.now() >= exp:
            # cleanup
            try:
                if os.path.isdir(path): shutil.rmtree(path)
                else: os.remove(path)
            except: pass
            drops.pop(drop_id,None); _save_drops(drops)
            return render_template("drop_expired.html", drop_id=drop_id), 404
        remaining=int((exp - datetime.datetime.now()).total_seconds())
        expiry_ts=int(exp.timestamp()*1000)
    except:
        remaining=0; expiry_ts=0
    # collect files recursively flat list with relative paths
    files=[]
    for root, dirs, fnames in os.walk(path):
        # skip tmp dirs
        dirs[:] = [d for d in dirs if not d.startswith(".tmp_")]
        for fn in fnames:
            if fn.startswith(".tmp_") or fn.endswith(".part") or fn==".meta.json":
                continue
            fp=os.path.join(root, fn)
            rel=os.path.relpath(fp, path)
            try:
                sz=round(os.stat(fp).st_size/(1024*1024),2)
            except: sz=0
            files.append({"name": rel.replace(os.sep, "/"), "size": sz})
    files.sort(key=lambda x: x["name"].lower())
    # determine if single file at root
    is_single = len(files)==1 and "/" not in files[0]["name"]
    if is_single:
        f=files[0]
        return render_template("drop_view_file.html", drop_id=drop_id, file=f, remaining=remaining, expiry_ts=expiry_ts, expiry_label=info.get("expiry_label","1d"))
    return render_template("drop_view_folder.html", drop_id=drop_id, files=files, remaining=remaining, expiry_ts=expiry_ts, expiry_label=info.get("expiry_label","1d"))

@app.route("/drop/<drop_id>/download/<path:filename>")
def drop_download(drop_id, filename):
    drop_id=drop_id.strip().upper()
    _cleanup_expired_drops()
    drops=_load_drops()
    info=drops.get(drop_id)
    if not info: return "Link wygasł",404
    path=info.get("path")
    if not path or not os.path.exists(path): return "Link wygasł",404
    try:
        if datetime.datetime.fromisoformat(info["expiry"]) < datetime.datetime.now():
            return "Link wygasł",404
    except: pass
    # build safe absolute path inside drop
    # filename may contain subfolders like "a/b.txt"
    parts=filename.split("/")
    clean=[secure_filename(p) for p in parts if p]
    clean_name="/".join(clean)
    # need to ensure file exists inside drop_path, handle original rel with possible secure sanitization
    # try exact then cleaned
    candidates=[os.path.join(path, filename), os.path.join(path, clean_name)]
    for cand in candidates:
        abs_cand=os.path.abspath(cand)
        if abs_cand.startswith(os.path.abspath(path)) and os.path.isfile(abs_cand):
            directory=os.path.dirname(abs_cand)
            fname=os.path.basename(abs_cand)
            return send_from_directory(directory, fname, as_attachment=True)
    # also search recursively if not found directly (fallback)
    for root, dirs, fnames in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".tmp_")]
        for fn in fnames:
            if fn==clean.split("/")[-1]:
                fp=os.path.join(root, fn)
                rel=os.path.relpath(fp, path).replace(os.sep,"/")
                if rel==filename or rel==clean_name:
                    return send_from_directory(os.path.dirname(fp), os.path.basename(fp), as_attachment=True)
    return "Nie znaleziono pliku",404

@app.route("/drop/<drop_id>/download_zip")
def drop_download_zip(drop_id):
    drop_id=drop_id.strip().upper()
    _cleanup_expired_drops()
    drops=_load_drops()
    info=drops.get(drop_id)
    if not info: return "Link wygasł",404
    path=info.get("path")
    if not path or not os.path.exists(path): return "Link wygasł",404
    try:
        if datetime.datetime.fromisoformat(info["expiry"]) < datetime.datetime.now():
            return "Link wygasł",404
    except: pass
    selected=request.args.getlist("files")
    sel=[p for p in selected if p] if selected else None
    # for single file drop, ignore filter
    if sel:
        # clean each relative path preserving folders (secure each part)
        clean_sel=[]
        for s in sel:
            parts=s.split("/")
            clean_sel.append("/".join([secure_filename(p) for p in parts if p]))
        sel=clean_sel
    return _zip_stream_response(path, path, selected_files=sel, zip_name=f"dinguz_drop_{drop_id}.zip")

@app.route("/api/drop/list", methods=["GET"])
def drop_list():
    email, uid, is_auth, per_file, account_max = get_drop_owner_and_limits()
    _cleanup_expired_drops()
    drops=_load_drops()
    now=datetime.datetime.now()
    out=[]
    for did, info in drops.items():
        if info.get("owner")!=email and info.get("owner_id")!=uid:
            continue
        exp_str=info.get("expiry")
        try:
            exp=datetime.datetime.fromisoformat(exp_str)
        except:
            continue
        if now >= exp:
            continue
        path=info.get("path")
        remaining=int((exp-now).total_seconds())
        # count files
        file_count=0
        total_mb=0
        if path and os.path.isdir(path):
            for root, dirs, fnames in os.walk(path):
                dirs[:] = [d for d in dirs if not d.startswith(".tmp_")]
                for fn in fnames:
                    if fn.startswith(".tmp_") or fn.endswith(".part") or fn==".meta.json":
                        continue
                    fp=os.path.join(root, fn)
                    try:
                        total_mb+=os.path.getsize(fp)
                    except: pass
                    file_count+=1
            total_mb=round(total_mb/(1024*1024),2)
        out.append({
            "drop_id": did,
            "drop_url": f"https://dinguzhosting.online/drop/{did}",
            "expiry": exp_str,
            "expiry_ts": int(exp.timestamp()*1000),
            "remaining": remaining,
            "expiry_label": info.get("expiry_label","1d"),
            "extended": bool(info.get("extended", False)),
            "file_count": file_count,
            "total_mb": total_mb,
        })
    out.sort(key=lambda x: x["expiry_ts"])
    return jsonify(out)

@app.route("/api/drop/add_time", methods=["POST"])
def drop_add_time():
    email, uid, is_auth, per_file, account_max = get_drop_owner_and_limits()
    data=request.get_json() or {}
    drop_id=(data.get("drop_id") or "").strip().upper()
    if not drop_id:
        return jsonify({"status":"error","message":"Brak drop_id"}),400
    drops=_load_drops()
    info=drops.get(drop_id)
    if not info:
        return jsonify({"status":"error","message":"Drop nie istnieje"}),404
    if info.get("owner")!=email and info.get("owner_id")!=uid:
        return jsonify({"status":"error","message":"Brak dostępu"}),403
    if info.get("extended"):
        return jsonify({"status":"error","message":"Już przedłużono - tylko raz"}),400
    try:
        exp=datetime.datetime.fromisoformat(info["expiry"])
    except:
        return jsonify({"status":"error","message":"Błąd daty"}),400
    now=datetime.datetime.now()
    remaining=(exp-now).total_seconds()
    if remaining <=0:
        return jsonify({"status":"error","message":"Wygasł"}),400
    if remaining > 3600:
        return jsonify({"status":"error","message":"Można dodać tylko gdy <1h do wygaśnięcia"}),400
    new_exp=exp + datetime.timedelta(seconds=3600)
    info["expiry"]=new_exp.isoformat()
    info["extended"]=True
    drops[drop_id]=info
    _save_drops(drops)
    return jsonify({"status":"ok","expiry": new_exp.isoformat(), "expiry_ts": int(new_exp.timestamp()*1000), "remaining": int((new_exp-now).total_seconds())})

@app.route("/api/drop/delete", methods=["POST"])
def drop_delete():
    email, uid, is_auth, per_file, account_max = get_drop_owner_and_limits()
    data=request.get_json() or {}
    drop_id=(data.get("drop_id") or "").strip().upper()
    if not drop_id:
        return jsonify({"status":"error","message":"Brak drop_id"}),400
    drops=_load_drops()
    info=drops.get(drop_id)
    if not info:
        return jsonify({"status":"error","message":"Drop nie istnieje"}),404
    if info.get("owner")!=email and info.get("owner_id")!=uid:
        return jsonify({"status":"error","message":"Brak dostępu"}),403
    path=info.get("path")
    if path and os.path.exists(path):
        try:
            if os.path.isdir(path): shutil.rmtree(path)
            else: os.remove(path)
        except Exception as e:
            return jsonify({"status":"error","message":str(e)}),500
    drops.pop(drop_id, None)
    _save_drops(drops)
    # cleanup any pending sessions for this drop
    for uid_key, meta in list(DROP_UPLOAD_SESSIONS.items()):
        if meta.get("drop_id")==drop_id:
            DROP_UPLOAD_SESSIONS.pop(uid_key, None)
    return jsonify({"status":"ok"})

# ==========================================
#            API I INNE
# ==========================================

@app.route("/api/internal/sync-password", methods=["POST"])
def sync_password():
    if request.headers.get("X-Sync-Token") != SYNC_TOKEN:
        return "Unauthorized", 401
    data = request.json
    email, payload = data.get("email"), data.get("payload")
    creds = {}
    if os.path.exists("konta.json"):
        with open("konta.json", "r") as f:
            creds = json.load(f)
    creds[email] = payload
    with open("konta.json", "w") as f:
        json.dump(creds, f)
    return "Success", 200

@app.route("/api/server/<identifier>/stats")
@requires_authorization
def server_stats(identifier):
    headers_client = {"Authorization": f"Bearer {PTERO_CLIENT_KEY}", "Accept": "application/json"}
    res = requests.get(f"{PTERO_URL}/api/client/servers/{identifier}/resources", headers=headers_client)
    return (res.json(), 200) if res.status_code == 200 else ({"error": "offline"}, 404)

@app.route("/server/<identifier>/power", methods=["POST"])
@requires_authorization
def power_server(identifier):
    action = request.form.get("action")
    headers_client = {"Authorization": f"Bearer {PTERO_CLIENT_KEY}", "Content-Type": "application/json"}
    requests.post(f"{PTERO_URL}/api/client/servers/{identifier}/power", json={"signal": action}, headers=headers_client)
    return redirect(url_for("dashboard"))

@app.route("/server/<identifier>/subdomain", methods=["POST"])
@requires_authorization
def create_subdomain(identifier):
    saved_subs = get_saved_subdomains()
    if str(identifier) in saved_subs:
        return "Błąd", 400
    subdomain = request.form.get("subdomain").lower().strip()
    if not re.match(r"^[a-z0-9-]+$", subdomain):
        return "Błąd", 400
    headers_client = {"Authorization": f"Bearer {PTERO_CLIENT_KEY}", "Accept": "application/json"}
    srv_req = requests.get(f"{PTERO_URL}/api/client/servers/{identifier}?include=allocations", headers=headers_client)
    port = srv_req.json()['attributes']['relationships']['allocations']['data'][0]['attributes']['port']
    cf_headers = {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}
    full_sub = f"{subdomain}.dinguzhosting.online"
    requests.post(f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records", json={"type": "A", "name": full_sub, "content": NODE_PUBLIC_IP, "ttl": 3600, "proxied": False}, headers=cf_headers)
    requests.post(f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records", json={"type": "SRV", "name": f"_minecraft._tcp.{full_sub}", "data": {"service": "_minecraft", "proto": "_tcp", "name": subdomain, "priority": 0, "weight": 5, "port": port, "target": full_sub}}, headers=cf_headers)
    save_subdomain(identifier, full_sub)
    return redirect(url_for("dashboard"))

# ==========================================
#          MODUŁ WIDEO (MEDIA PLAYER)
# ==========================================

VIDEO_SHARES_FILE = "video_shares.json"
VIDEO_UPLOAD_FOLDER = 'video_uploads'
os.makedirs(VIDEO_UPLOAD_FOLDER, exist_ok=True)

def get_user_video_folder(email):
    safe_email = re.sub(r'[^a-zA-Z0-9]', '_', email)
    folder_path = os.path.join(VIDEO_UPLOAD_FOLDER, safe_email)
    os.makedirs(folder_path, exist_ok=True)
    return folder_path

@app.route("/video", methods=["GET"])
@requires_authorization
def video_dashboard():
    user = get_discord_user()
    base_folder = get_user_video_folder(user['email'])
    current_rel_path = request.args.get("path", "")
    current_folder = get_safe_path(base_folder, current_rel_path)
    
    if not os.path.exists(current_folder):
        os.makedirs(current_folder, exist_ok=True)
        
    items = []
    for filename in os.listdir(current_folder):
        path = os.path.join(current_folder, filename)
        is_dir = os.path.isdir(path)
        
        # Filtrowanie obsługiwanych rozszerzeń oraz folderów
        if is_dir or filename.lower().endswith(('.mp4', '.webm', '.ogg', '.mov', '.png', '.jpg', '.jpeg', '.gif', '.webp')):
            if not filename.endswith('.part'):
                stats = os.stat(path)
                items.append({
                    "name": filename,
                    "is_dir": is_dir,
                    "size": round(get_dir_size(path) / (1024 * 1024), 2) if is_dir else round(stats.st_size / (1024 * 1024), 2),
                    "date": datetime.datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M')
                })
                
    # Sortujemy: foldery na górze
    items.sort(key=lambda x: (not x["is_dir"], x["date"]), reverse=True)
    parent_path = os.path.dirname(current_rel_path.strip('/')) if current_rel_path.strip('/') else ""
    
    return render_template("video.html", discord_user=user, items=items, current_path=current_rel_path, parent_path=parent_path)

@app.route("/video/create_folder", methods=["POST"])
@requires_authorization
def create_video_folder():
    user = get_discord_user()
    base_folder = get_user_video_folder(user['email'])
    current_rel_path = request.form.get("path", "")
    folder_name = secure_filename(request.form.get("folder_name", ""))
    
    if folder_name:
        target = get_safe_path(base_folder, current_rel_path)
        os.makedirs(os.path.join(target, folder_name), exist_ok=True)
    return redirect(url_for("video_dashboard", path=current_rel_path))

@app.route("/video/download")
@requires_authorization
def download_media_file():
    user = get_discord_user()
    base_folder = get_user_video_folder(user['email'])
    current_rel_path = request.args.get("path", "")
    filename = secure_filename(request.args.get("file", ""))
    target_folder = get_safe_path(base_folder, current_rel_path)
    return send_from_directory(target_folder, filename, as_attachment=True)

@app.route("/api/upload_video_chunk", methods=["POST"])
@requires_authorization
def upload_video_chunk():
    user = get_discord_user()
    base_folder = get_user_video_folder(user['email'])
    
    if get_dir_size(base_folder) >= USER_QUOTA_LIMIT:
        return jsonify({"status": "error", "message": "Limit miejsca wyczerpany!"}), 400

    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "Brak pliku"}), 400
    file_chunk = request.files['file']
    
    filename = request.form.get('filename') or file_chunk.filename
    if not filename:
        return jsonify({"status": "error", "message": "Brak nazwy pliku"}), 400
    filename = secure_filename(filename)

    chunk_index = request.form.get('chunkIndex') or request.form.get('chunk_index')
    total_chunks = request.form.get('totalChunks') or request.form.get('total_chunks')
    
    current_rel_path = request.form.get("path", "")
    target_folder = get_safe_path(base_folder, current_rel_path)
    os.makedirs(target_folder, exist_ok=True)

    temp_path = os.path.join(target_folder, f"{filename}.part")
    final_path = os.path.join(target_folder, filename)
    
    with open(temp_path, "ab") as f:
        f.write(file_chunk.read())

    if int(chunk_index) == int(total_chunks) - 1:
        if os.path.exists(final_path):
            os.remove(final_path)
        os.rename(temp_path, final_path)
        return jsonify({"status": "completed", "filename": filename})
    
    return jsonify({"status": "chunk_received", "chunk": chunk_index})

@app.route("/video/stream")
def stream_video():
    filename = secure_filename(request.args.get("file", ""))
    if not filename:
        return "Brak pliku", 400
    current_rel_path = request.args.get("path", "")
    user = get_discord_user()
    # Jeśli użytkownik zalogowany, spróbuj jego folderu najpierw
    if user and 'email' in user:
        base_folder = get_user_video_folder(user['email'])
        target_folder = get_safe_path(base_folder, current_rel_path)
        full_path = os.path.join(target_folder, filename)
        if os.path.exists(full_path):
            return send_from_directory(target_folder, filename)
    # Fallback: publiczny dostęp dla docsów i współdzielonych zasobów
    # Szukaj pliku w całym VIDEO_UPLOAD_FOLDER (dla vd2_docs_icon.png itp.)
    if not current_rel_path:
        for root, dirs, files in os.walk(VIDEO_UPLOAD_FOLDER):
            if filename in files:
                return send_from_directory(root, filename)
    else:
        # Jeśli podano path, spróbuj znaleźć w dowolnym folderze użytkownika
        for root, dirs, files in os.walk(VIDEO_UPLOAD_FOLDER):
            # Sprawdź czy plik istnieje w tym path
            candidate = os.path.join(root, current_rel_path, filename)
            if os.path.exists(candidate):
                return send_from_directory(os.path.join(root, current_rel_path), filename)
            # Fallback: sam plik bez path
            if filename in files and root.endswith(current_rel_path.replace('/', os.sep)):
                return send_from_directory(root, filename)
    return "Brak pliku", 404

@app.route("/video/stream_shared/<video_id>.mp4")
def stream_shared_mp4(video_id):
    path = get_video_path(video_id)

    return send_file(
        path,
        mimetype="video/mp4",
        as_attachment=False,
        conditional=True
    )

@app.route("/video/share", methods=["POST"])
@requires_authorization
def share_video():
    user = get_discord_user()
    base_folder = get_user_video_folder(user['email'])
    data = request.json
    current_rel_path = data.get("path", "")
    filename = data.get("file", "")
    
    target = get_safe_path(base_folder, os.path.join(current_rel_path, secure_filename(filename)))
    if not os.path.exists(target):
        return jsonify({"error": "Błąd, nie znaleziono pliku lub folderu"}), 400
    
    share_id = generate_share_id()
    shares = {}
    if os.path.exists(VIDEO_SHARES_FILE):
        with open(VIDEO_SHARES_FILE, "r") as f:
            shares = json.load(f)
        
    shares[share_id] = target
    with open(VIDEO_SHARES_FILE, "w") as f:
        json.dump(shares, f)
    
    return jsonify({"share_url": f"https://dinguzhosting.online/video/watch/{share_id}"})

@app.route("/video/watch/<share_id>")
def watch_shared_video(share_id):
    if not os.path.exists(VIDEO_SHARES_FILE):
        return "Błąd", 404
    with open(VIDEO_SHARES_FILE, "r") as f:
        shares = json.load(f)
    file_path = shares.get(share_id)
    
    if not file_path or not os.path.exists(file_path):
        return "Link wygasł albo został usunięty", 404
        
    is_dir = os.path.isdir(file_path)
    filename = os.path.basename(file_path)
    
    files_list = []
    if is_dir:
        for f in os.listdir(file_path):
            fp = os.path.join(file_path, f)
            if os.path.isfile(fp):
                files_list.append({"name": f, "size": round(os.stat(fp).st_size / (1024 * 1024), 2)})
                
    return render_template("watch.html", share_id=share_id, filename=filename, is_dir=is_dir, files=files_list)

@app.route("/video/stream_shared/<share_id>")
def stream_shared_video(share_id):
    if not os.path.exists(VIDEO_SHARES_FILE):
        return "Błąd", 404
    with open(VIDEO_SHARES_FILE, "r") as f:
        shares = json.load(f)
    file_path = shares.get(share_id)
    if not file_path:
        return "Błąd", 404
    # Fallback: jeśli ścieżka z JSON nie istnieje (np. /home/container/ na innym hostcie), spróbuj znaleźć plik po nazwie w VIDEO_UPLOAD_FOLDER
    if not os.path.exists(file_path):
        # próba znalezienia po basename
        basename = os.path.basename(file_path)
        found = None
        # 1) szukaj w VIDEO_UPLOAD_FOLDER rekurencyjnie
        for root, dirs, files in os.walk(VIDEO_UPLOAD_FOLDER):
            if basename in files:
                found = os.path.join(root, basename)
                break
        # 2) szukaj w dinguzplus_movies jako fallback (dla testów)
        if not found and os.path.isdir(DINGUZPLUS_MOVIES):
            for fname in os.listdir(DINGUZPLUS_MOVIES):
                if fname == basename:
                    found = os.path.join(DINGUZPLUS_MOVIES, fname)
                    break
        if found and os.path.exists(found):
            file_path = found
        else:
            return "Błąd", 404
    
    req_file = request.args.get("file")
    
    # Ustalamy właściwy plik i folder
    if os.path.isdir(file_path) and req_file:
        folder = file_path
        filename = secure_filename(req_file)
        # fallback jak folder nie istnieje
        if not os.path.exists(os.path.join(folder, filename)):
            # szukaj rekurencyjnie basename
            for root, dirs, files in os.walk(folder):
                if filename in files:
                    folder = root
                    break
    elif os.path.isfile(file_path):
        folder = os.path.dirname(file_path)
        filename = os.path.basename(file_path)
    else:
        return "Błąd", 404

    # Użyj stream z Range support (jak dinguzplus) zamiast prostego send_from_directory
    full_path = os.path.join(folder, filename)
    if not os.path.exists(full_path):
        return "Błąd", 404
    # Dla wideo użyj _dinguzplus_stream_file żeby mieć 206 Range, ETag, HEAD, CORS
    try:
        resp = _dinguzplus_stream_file(full_path, request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS, HEAD"
        resp.headers["Access-Control-Allow-Headers"] = "Range, Content-Type"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Range, Content-Length, Accept-Ranges, ETag, Last-Modified"
        return resp
    except Exception:
        response = send_from_directory(folder, filename, conditional=True)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS, HEAD"
        response.headers["Access-Control-Allow-Headers"] = "Range, Content-Type"
        response.headers["Access-Control-Expose-Headers"] = "Content-Range, Content-Length, Accept-Ranges"
        return response
# ==========================================
#           MODUŁ HOSTOWANIA MODÓW VD2
# ==========================================

MODS_STORAGE_FOLDER = 'mods_storage'
os.makedirs(MODS_STORAGE_FOLDER, exist_ok=True)

MAX_MODS_PER_USER = 10
MAX_MOD_SIZE_MB = 50
MAX_MOD_SIZE_BYTES = MAX_MOD_SIZE_MB * 1024 * 1024

def get_user_mod_folder(user_id):
    """Zwraca ścieżkę do folderu użytkownika na podstawie jego Discord ID."""
    folder_path = os.path.join(MODS_STORAGE_FOLDER, str(user_id))
    os.makedirs(folder_path, exist_ok=True)
    return folder_path

@app.route("/vd2_my_mods")
@requires_authorization
def get_user_mods():
    user = get_discord_user()
    folder = get_user_mod_folder(user['id'])
    mods = []
    for filename in os.listdir(folder):
        path = os.path.join(folder, filename)
        if os.path.isfile(path):
            stats = os.stat(path)
            mods.append({
                "name": filename,
                "size": round(stats.st_size / (1024 * 1024), 2),
                "date": datetime.datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M'),
                "url": f"https://dinguzhosting.online/vd2_download/{user['id']}/{filename}"
            })
    return jsonify(mods)

@app.route("/vd2_upload", methods=["POST"])
@requires_authorization
def vd2_upload():
    user = get_discord_user()
    if 'mod_file' not in request.files:
        return jsonify({"error": "Brak pliku"}), 400
        
    file = request.files['mod_file']
    if file.filename == '':
        return jsonify({"error": "Nie wybrano pliku"}), 400
        
    folder = get_user_mod_folder(user['id'])
    
    existing_files = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
    if len(existing_files) >= MAX_MODS_PER_USER:
        return jsonify({"error": "Osiągnięto limit 10 modów. Usuń stary mod, by wgrać nowy."}), 400

    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0, 0)
    
    if file_length > MAX_MOD_SIZE_BYTES:
        return jsonify({"error": "Plik przekracza limit 50MB."}), 400

    file_path = os.path.join(folder, secure_filename(file.filename))
    file.save(file_path)
    return jsonify({"success": True})

@app.route("/vd2_update", methods=["POST"])
@requires_authorization
def vd2_update():
    user = get_discord_user()
    if 'mod_file' not in request.files:
        return jsonify({"error": "Brak pliku"}), 400
        
    file = request.files['mod_file']
    if file.filename == '':
        return jsonify({"error": "Nie wybrano pliku"}), 400
        
    # Pobieramy nazwę pliku do nadpisania z formularza
    target_filename = secure_filename(request.form.get("target_filename", ""))
    if not target_filename:
        return jsonify({"error": "Brak nazwy pliku do nadpisania"}), 400

    folder = get_user_mod_folder(user['id'])
    file_path = os.path.join(folder, target_filename)

    # Sprawdzenie, czy plik istnieje (czyli czy aktualizujemy istniejący)
    if not os.path.exists(file_path):
        return jsonify({"error": "Plik nie istnieje, użyj Upload zamiast Update"}), 404

    # Walidacja rozmiaru
    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0, 0)
    if file_length > MAX_MOD_SIZE_BYTES:
        return jsonify({"error": "Plik przekracza limit 50MB."}), 400

    # Zapisujemy nowy plik nadpisując stary
    file.save(file_path)
    return jsonify({"success": True, "message": "Mod zaktualizowany pomyślnie"})

@app.route("/vd2_delete", methods=["POST"])
@requires_authorization
def vd2_delete():
    user = get_discord_user()
    data = request.json
    filename = secure_filename(data.get("filename", ""))
    
    folder = get_user_mod_folder(user['id'])
    file_path = os.path.join(folder, filename)
    
    if os.path.exists(file_path) and os.path.isfile(file_path):
        os.remove(file_path)
        return jsonify({"success": True})
        
    return jsonify({"error": "Plik nie istnieje."}), 404

@app.route("/vd2_download/<user_id>/<filename>")
def vd2_download(user_id, filename):
    folder = os.path.join(MODS_STORAGE_FOLDER, user_id)
    filepath = os.path.join(folder, secure_filename(filename))
    if not os.path.exists(filepath):
        return "File not found", 404
    
    response = send_file(
        filepath,
        mimetype='application/octet-stream',
        as_attachment=False
    )
    # WYMUSZAMY inline, a nie attachment
    response.headers['Content-Disposition'] = 'inline'
    return response

# ==========================================
#               DINGUZ+  VOD PLATFORM
# ==========================================
_DINGUZPLUS_BASE = os.path.dirname(os.path.abspath(__file__))
DINGUZPLUS_MOVIES = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_movies")
DINGUZPLUS_RESOURCES = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_resources")
DINGUZPLUS_THUMBNAILS = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_thumbnails")
DINGUZPLUS_TRAILERS = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_trailers")
DINGUZPLUS_SUBTITLES = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_subtitles")
DINGUZPLUS_SERIES = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_series")
DINGUZPLUS_PREVIEWS = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_previews")
DINGUZPLUS_PROFILES = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_profiles.json")
DINGUZPLUS_PROGRESS = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_progress.json")
DINGUZPLUS_CONFIG = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_config.json")
DINGUZPLUS_MYLIST = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_mylist.json")
DINGUZPLUS_RATINGS = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_ratings.json")
DINGUZPLUS_VIEWS = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_views.json")
DINGUZPLUS_HISTORY = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_history.json")
DINGUZPLUS_CATEGORIES = os.path.join(_DINGUZPLUS_BASE, "dinguzplus_categories.json")
FFMPEG_BIN = os.environ.get('FFMPEG_BIN') or 'ffmpeg'
_DINGUZPLUS_CONTENT_TAGS = {
    3: [],
    7: [],
    13: ["Strach","Przemoc","Łagodna przemoc","Mocny język","Używanie alkoholu","Używanie substancji","Treści seksualne","Nagość"],
    16: ["Brutalna przemoc","Przemoc","Mocny język","Wulgarny język","Nagość","Treści seksualne","Seks","Używanie alkoholu","Używanie substancji","Narkotyki","Samookaleczenia","Tematy budzące niepokój","Horror","Strach"],
    18: ["Brutalna przemoc","Skrajna przemoc","Krew i gore","Mocny język","Wulgarny język","Nagość","Nagość seksualna","Treści seksualne","Seks","Używanie alkoholu","Używanie substancji","Narkotyki","Samookaleczenia","Tematy dla dorosłych","Tematy budzące niepokój","Horror","Strach"]
}
os.makedirs(DINGUZPLUS_MOVIES, exist_ok=True)
os.makedirs(DINGUZPLUS_RESOURCES, exist_ok=True)
os.makedirs(DINGUZPLUS_THUMBNAILS, exist_ok=True)
os.makedirs(DINGUZPLUS_TRAILERS, exist_ok=True)
os.makedirs(DINGUZPLUS_SUBTITLES, exist_ok=True)
os.makedirs(DINGUZPLUS_SERIES, exist_ok=True)
os.makedirs(DINGUZPLUS_PREVIEWS, exist_ok=True)
DINGUZPLUS_UPLOAD_SESSIONS = {}

def _dinguzplus_ensure_dirs():
    os.makedirs(DINGUZPLUS_MOVIES, exist_ok=True)
    os.makedirs(DINGUZPLUS_RESOURCES, exist_ok=True)
    os.makedirs(DINGUZPLUS_THUMBNAILS, exist_ok=True)
    os.makedirs(DINGUZPLUS_TRAILERS, exist_ok=True)
    os.makedirs(DINGUZPLUS_SUBTITLES, exist_ok=True)
    os.makedirs(DINGUZPLUS_SERIES, exist_ok=True)
    os.makedirs(DINGUZPLUS_PREVIEWS, exist_ok=True)

def _dinguzplus_safe_id(mid):
    if not mid: return None
    mid = secure_filename(mid)
    mid = mid.strip()
    if not mid: return None
    if not re.match(r'^[a-zA-Z0-9._-]+$', mid):
        return None
    # no traversal
    if '..' in mid or '/' in mid or '\\' in mid:
        return None
    return mid

def _dinguzplus_pretty_name(mid):
    s = mid.replace('_',' ').replace('-',' ').replace('.',' ')
    s = re.sub(r'\s+', ' ', s).strip()
    # Title case but keep acronyms
    return ' '.join(w.capitalize() for w in s.split(' '))

def _dinguzplus_duration(path):
    try:
        import subprocess
        # ffprobe if available
        r = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1', path], capture_output=True, text=True, timeout=5)
        if r.returncode==0 and r.stdout.strip():
            return float(r.stdout.strip())
    except: pass
    return 0

# --- Thumbnail helpers (custom + vertical, any image format -> PNG canonical) ---
_DINGUZPLUS_THUMB_EXTS = ['.png','.jpg','.jpeg','.webp']
def _dinguzplus_find_thumb(safe, vertical=False):
    base = safe + ("_vertical" if vertical else "")
    for ext in _DINGUZPLUS_THUMB_EXTS:
        p = os.path.join(DINGUZPLUS_THUMBNAILS, base + ext)
        if os.path.exists(p):
            return p
    # also check legacy .png exact
    p = os.path.join(DINGUZPLUS_THUMBNAILS, base + ".png")
    return p if os.path.exists(p) else None

def _dinguzplus_thumb_mtime(safe, vertical=False):
    p = _dinguzplus_find_thumb(safe, vertical)
    if p and os.path.exists(p):
        try: return int(os.path.getmtime(p))
        except: return 0
    return 0

def _dinguzplus_save_thumb_file(fs, dest_base_without_ext):
    """Save uploaded image FileStorage as PNG canonical.
    Accepts png/jpg/jpeg/webp, converts to PNG via Pillow or ffmpeg if needed.
    dest_base_without_ext = full path without extension, e.g. .../thumbnails/<id>
    Returns True on success.
    """
    if not fs or not fs.filename:
        return False
    ext = os.path.splitext(fs.filename)[1].lower()
    if ext not in _DINGUZPLUS_THUMB_EXTS:
        return False
    dest_png = dest_base_without_ext + ".png"
    # png direct save
    if ext == '.png':
        try: fs.save(dest_png); return os.path.exists(dest_png)
        except: return False
    # non-png: save temp then convert
    tmp = dest_png + ".upload.tmp" + ext
    try:
        fs.save(tmp)
        # try Pillow
        try:
            from PIL import Image
            with Image.open(tmp) as im:
                if im.mode in ('RGBA','LA','P'):
                    # composite over black for consistency
                    bg = Image.new('RGB', im.size, (0,0,0))
                    if im.mode == 'P':
                        im = im.convert('RGBA')
                    bg.paste(im, mask=im.split()[-1] if im.mode=='RGBA' else None)
                    im = bg
                else:
                    im = im.convert('RGB')
                im.save(dest_png, 'PNG')
            try: os.remove(tmp)
            except: pass
            return os.path.exists(dest_png)
        except Exception:
            pass
        # fallback ffmpeg
        try:
            r = subprocess.run([FFMPEG_BIN, '-y', '-i', tmp, '-frames:v', '1', dest_png], capture_output=True, timeout=15)
            if os.path.exists(dest_png) and os.path.getsize(dest_png) > 0:
                try: os.remove(tmp)
                except: pass
                return True
        except: pass
        # last resort: copy as png (browser may still render jpeg as png via sniffing? but try)
        try:
            shutil.copy(tmp, dest_png)
            try: os.remove(tmp)
            except: pass
            return True
        except: pass
        try: os.remove(tmp)
        except: pass
        return False
    except Exception:
        try: os.remove(tmp)
        except: pass
        return False

def _dinguzplus_metadata_path(mid):
    safe = _dinguzplus_safe_id(mid)
    if not safe: return None
    return os.path.join(DINGUZPLUS_RESOURCES, safe + ".json")

def _dinguzplus_ensure_metadata(mid, filename=None):
    p = _dinguzplus_metadata_path(mid)
    if not p: return None
    pretty = _dinguzplus_pretty_name(mid)
    # infer filename if not provided
    if not filename:
        mp = _dinguzplus_movie_path(mid)
        if mp:
            filename = os.path.basename(mp)
        else:
            filename = mid + ".mp4"
    if os.path.exists(p):
        try:
            with open(p,'r') as f:
                data=json.load(f)
            changed=False
            if 'age_restriction' not in data:
                data['age_restriction']=16; changed=True
            if 'file_name' not in data:
                data['file_name']=filename; changed=True
            if 'movie_title' not in data:
                data['movie_title']=pretty; changed=True
            if 'premiere_at' not in data:
                data['premiere_at']=None; changed=True
            if 'section' not in data:
                data['section']='Filmy'; changed=True
            if 'order' not in data:
                data['order']=999; changed=True
            if 'description' not in data:
                data['description']=''; changed=True
            if 'categories' not in data:
                # migrate from section
                sec=data.get('section','Filmy')
                data['categories']=[sec] if sec else ['Filmy']; changed=True
            if 'trailer' not in data:
                data['trailer']=None; changed=True
            if 'subtitles' not in data:
                data['subtitles']=[]; changed=True
            if changed:
                with open(p,'w') as f: json.dump(data,f,indent=2)
            return data
        except:
            pass
    data={"age_restriction":16, "file_name": filename, "movie_title": pretty, "premiere_at": None, "section": "Filmy", "categories": ["Filmy"], "order": 999, "description": "", "trailer": None, "subtitles": []}
    try:
        with open(p,'w') as f:
            json.dump(data,f,indent=2)
    except: pass
    return data

def _dinguzplus_get_metadata(mid):
    p=_dinguzplus_metadata_path(mid)
    if not p or not os.path.exists(p):
        return _dinguzplus_ensure_metadata(mid)
    try:
        with open(p,'r') as f:
            data=json.load(f)
        changed=False
        if 'age_restriction' not in data:
            data['age_restriction']=16; changed=True
        if 'file_name' not in data:
            mp = _dinguzplus_movie_path(mid)
            data['file_name']= os.path.basename(mp) if mp else mid+".mp4"
            changed=True
        if 'movie_title' not in data:
            data['movie_title']= _dinguzplus_pretty_name(mid)
            changed=True
        if 'premiere_at' not in data:
            data['premiere_at']=None; changed=True
        if 'section' not in data:
            data['section']='Filmy'; changed=True
        if 'order' not in data:
            data['order']=999; changed=True
        if 'content_tags' not in data:
            data['content_tags']=[]; changed=True
        # filter content_tags based on age
        try:
            allowed=_DINGUZPLUS_CONTENT_TAGS.get(int(data.get('age_restriction',16)), [])
            if int(data.get('age_restriction',16)) in [3,7]:
                if data.get('content_tags'):
                    data['content_tags']=[]; changed=True
            else:
                # remove invalid tags
                orig=data.get('content_tags',[])
                filtered=[t for t in orig if t in allowed]
                if filtered!=orig:
                    data['content_tags']=filtered; changed=True
        except: pass
        if 'description' not in data:
            data['description']=''; changed=True
        if 'categories' not in data:
            sec=data.get('section','Filmy')
            data['categories']=[sec] if sec else ['Filmy']; changed=True
        if 'trailer' not in data:
            data['trailer']=None; changed=True
        if 'subtitles' not in data:
            data['subtitles']=[]; changed=True
        if changed:
            with open(p,'w') as f: json.dump(data,f,indent=2)
        return data
    except:
        mp = _dinguzplus_movie_path(mid)
        fn = os.path.basename(mp) if mp else mid+".mp4"
        return {"age_restriction":16, "file_name": fn, "movie_title": _dinguzplus_pretty_name(mid), "premiere_at": None, "section": "Filmy", "categories": ["Filmy"], "order": 999, "description": "", "trailer": None, "subtitles": [], "is_premiered": True}

def _dinguzplus_list_movies():
    _dinguzplus_ensure_dirs()
    movies=[]
    if not os.path.exists(DINGUZPLUS_MOVIES):
        return movies
    for fname in os.listdir(DINGUZPLUS_MOVIES):
        fpath=os.path.join(DINGUZPLUS_MOVIES, fname)
        if not os.path.isfile(fpath): continue
        if not fname.lower().endswith('.mp4'): continue
        base=os.path.splitext(fname)[0]
        safe=_dinguzplus_safe_id(base)
        if not safe: continue
        # ensure metadata exists with filename and title
        meta=_dinguzplus_ensure_metadata(safe, fname)
        # thumbnail/poster check - thumbnails folder first (any image ext, canonical PNG)
        poster=None
        poster_vertical=None
        poster_mtime=0
        poster_vertical_mtime=0
        thumb_p=_dinguzplus_find_thumb(safe, vertical=False)
        if thumb_p:
            poster=f"/dinguzplus/thumbnail/{safe}?v={_dinguzplus_thumb_mtime(safe, False)}"
            poster_mtime=_dinguzplus_thumb_mtime(safe, False)
        else:
            for ext in ['.jpg','.jpeg','.png','.webp']:
                pp=os.path.join(DINGUZPLUS_RESOURCES, safe+ext)
                if os.path.exists(pp):
                    poster=f"/dinguzplus/poster/{safe}"
                    try: poster_mtime=int(os.path.getmtime(pp))
                    except: poster_mtime=0
                    break
        # vertical thumbnail
        vert_p=_dinguzplus_find_thumb(safe, vertical=True)
        if vert_p:
            poster_vertical=f"/dinguzplus/thumbnail_vertical/{safe}?v={_dinguzplus_thumb_mtime(safe, True)}"
            poster_vertical_mtime=_dinguzplus_thumb_mtime(safe, True)
        else:
            # fallback to horizontal if vertical not exists
            poster_vertical=poster
            poster_vertical_mtime=poster_mtime
        size_mb=round(os.path.getsize(fpath)/(1024*1024),2)
        # duration
        dur=meta.get('duration',0)
        if not dur:
            dur=_dinguzplus_duration(fpath)
            if dur:
                meta['duration']=round(dur,1)
                try:
                    with open(_dinguzplus_metadata_path(safe),'r') as f: cur=json.load(f)
                    cur['duration']=meta['duration']
                    with open(_dinguzplus_metadata_path(safe),'w') as f: json.dump(cur,f,indent=2)
                except: pass
        # trailer check
        has_trailer = os.path.exists(os.path.join(DINGUZPLUS_TRAILERS, safe+".mp4")) or os.path.exists(os.path.join(DINGUZPLUS_TRAILERS, safe+"_trailer.mp4"))
        trailer_url=f"/dinguzplus/trailer/{safe}" if has_trailer else None
        # subtitles check
        subs=[]
        sub_dir=os.path.join(DINGUZPLUS_SUBTITLES, safe)
        if os.path.isdir(sub_dir):
            for sf in os.listdir(sub_dir):
                if sf.lower().endswith('.vtt'):
                    lang=os.path.splitext(sf)[0]
                    subs.append(lang)
        # categories
        cats=meta.get('categories') or [meta.get('section','Filmy')]
        if isinstance(cats, str): cats=[cats]
        # NEW badge - added within 3 days (file mtime)
        try:
            mtime=os.path.getmtime(fpath)
            is_new=(datetime.datetime.now().timestamp() - mtime) < 3*24*3600
        except: is_new=False
        # views
        views_data=_dinguzplus_load_views()
        views=int(views_data.get(safe,0))
        # ratings
        ratings_data=_dinguzplus_load_ratings()
        r_entry=ratings_data.get(safe,{})
        likes=sum(1 for v in r_entry.values() if v=="like")
        dislikes=sum(1 for v in r_entry.values() if v=="dislike")
        movies.append({
            "id": safe,
            "filename": fname,
            "file_name": meta.get('file_name', fname),
            "movie_title": meta.get('movie_title', _dinguzplus_pretty_name(safe)),
            "pretty": meta.get('movie_title', _dinguzplus_pretty_name(safe)),
            "size_mb": size_mb,
            "duration": meta.get('duration',0),
            "age": meta.get('age_restriction',16),
            "poster": poster,
            "poster_vertical": poster_vertical,
            "poster_mtime": poster_mtime,
            "poster_vertical_mtime": poster_vertical_mtime,
            "has_poster": bool(poster),
            "has_poster_vertical": bool(vert_p is not None),
            "premiere_at": meta.get('premiere_at'),
            "section": meta.get('section','Filmy'),
            "categories": cats,
            "order": int(meta.get('order',999)),
            "description": meta.get('description',''),
            "is_premiered": _dinguzplus_is_premiered(meta),
            "has_trailer": has_trailer,
            "trailer": trailer_url,
            "subtitles": subs,
            "is_new": is_new,
            "views": views,
            "likes": likes,
            "dislikes": dislikes
        })
    # sort by order then pretty
    try:
        movies.sort(key=lambda x: (x['order'], x['pretty'].lower()))
    except:
        movies.sort(key=lambda x: x['pretty'].lower())
    return movies

def _dinguzplus_movie_path(mid):
    safe=_dinguzplus_safe_id(mid)
    if not safe: return None
    # find actual file case-insensitive
    for fname in os.listdir(DINGUZPLUS_MOVIES):
        if os.path.splitext(fname)[0]==safe and fname.lower().endswith('.mp4'):
            return os.path.join(DINGUZPLUS_MOVIES, fname)
    # fallback direct
    p=os.path.join(DINGUZPLUS_MOVIES, safe+".mp4")
    if os.path.exists(p):
        return p
    return None

# profiles
def _dinguzplus_load_profiles():
    if os.path.exists(DINGUZPLUS_PROFILES):
        try:
            with open(DINGUZPLUS_PROFILES,'r') as f:
                return json.load(f)
        except: return {}
    return {}
def _dinguzplus_save_profiles(data):
    with open(DINGUZPLUS_PROFILES,'w') as f:
        json.dump(data,f,indent=2)
def _dinguzplus_load_progress():
    if os.path.exists(DINGUZPLUS_PROGRESS):
        try:
            with open(DINGUZPLUS_PROGRESS,'r') as f:
                return json.load(f)
        except: return {}
    return {}
def _dinguzplus_save_progress(data):
    with open(DINGUZPLUS_PROGRESS,'w') as f:
        json.dump(data,f,indent=2)

def _dinguzplus_get_active_profile():
    return session.get('dinguzplus_profile_id')

# --- PREVIEWS (timeline thumbnails every 15s) ---
import queue as _queue
import threading as _threading
_DINGUZPLUS_PREVIEW_QUEUE = _queue.Queue()
_DINGUZPLUS_PREVIEW_RUNNING = False

def _dinguzplus_preview_dir(mid):
    """Return directory for preview thumbnails. Supports composite ID for series episodes: sid_season_N_ep."""
    if not mid: return None
    if mid == '__intro__':
        return os.path.join(DINGUZPLUS_PREVIEWS, '_intro')
    safe=_dinguzplus_safe_id(mid)
    if not safe: return None
    cm=__import__('re').match(r'^(.+?)_season_(\d+?)_(.+)$', safe)
    if cm:
        sid, sn, ep = cm.group(1), cm.group(2), cm.group(3)
        return os.path.join(DINGUZPLUS_PREVIEWS, sid, f"season_{sn}", ep)
    return os.path.join(DINGUZPLUS_PREVIEWS, safe)

def _dinguzplus_preview_path(mid, sec):
    d=_dinguzplus_preview_dir(mid)
    if not d: return None
    # round to nearest 15s for filename: 0000, 0015, 0030, ...
    rounded=int(round(sec/15)*15)
    return os.path.join(d, f"{rounded:04d}.webp")

def _dinguzplus_resolve_mp4(mid):
    """Return absolute path to MP4 for a movie id, supporting film, series episode, and composite id."""
    safe=_dinguzplus_safe_id(mid)
    if not safe: return None
    p=_dinguzplus_movie_path(safe)
    if p and os.path.exists(p): return p
    cm=__import__('re').match(r'^(.+?)_season_(\d+?)_(.+)$', safe)
    if cm:
        csid, csn, cep = cm.group(1), cm.group(2), cm.group(3)
        p2=os.path.join(DINGUZPLUS_SERIES, csid, f"season_{csn}", f"{cep}.mp4")
        if os.path.exists(p2): return p2
    return None

def _dinguzplus_generate_preview(mid, sec):
    """Auto-generate a single preview thumbnail at given sec (rounded to 15s) using ffmpeg. Returns True on success."""
    if mid == '__intro__':
        src=os.path.join(DINGUZPLUS_RESOURCES, "intro.mp4")
    else:
        src=_dinguzplus_resolve_mp4(mid)
    if not src or not os.path.exists(src): return False
    target=_dinguzplus_preview_path(mid, sec)
    if not target: return False
    if os.path.exists(target): return True
    os.makedirs(os.path.dirname(target), exist_ok=True)
    rounded=int(round(sec/15)*15)
    try:
        r=subprocess.run([
            FFMPEG_BIN, '-y', '-ss', str(rounded), '-i', src,
            '-frames:v', '1', '-q:v', '5', '-vf', 'scale=320:-1', target
        ], capture_output=True, timeout=30)
        return os.path.exists(target)
    except Exception:
        return False

def _dinguzplus_ensure_preview_dir(mid):
    d=_dinguzplus_preview_dir(mid)
    if d:
        os.makedirs(d, exist_ok=True)
    return d

def _dinguzplus_ensure_episode_thumbnail(sid, season, ep_id):
    """Ensure deterministic thumbnail for episode exists. Returns path or None.
    Deterministic: based on duration * 0.1 (clamped 2..10 sec), same episode always same thumb.
    Custom thumbnail has priority - if custom file exists, do nothing."""
    safe=_dinguzplus_safe_id(sid)
    safe_season=secure_filename(season)
    safe_ep=secure_filename(ep_id)
    thumb_path=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe, f"{safe_season}_{safe_ep}.png")
    # custom thumbnail has priority
    if os.path.exists(thumb_path):
        return thumb_path
    # find video file
    season_dir=os.path.join(DINGUZPLUS_SERIES, safe, safe_season)
    mp4_path=os.path.join(season_dir, f"{safe_ep}.mp4")
    if not os.path.exists(mp4_path):
        return None
    # get duration deterministically
    duration=0
    # try episode meta duration first
    try:
        with open(os.path.join(season_dir, f"{safe_ep}.json"),'r') as f:
            duration=float(json.load(f).get('duration') or 0)
    except:
        duration=0
    if not duration:
        duration=_dinguzplus_duration(mp4_path)
    # deterministic timestamp: 10% of duration, clamped 2..10 sec, fallback 5 sec
    if duration and duration > 0:
        sec=int(duration * 0.1)
        sec=max(2, min(sec, 10))
        if sec >= duration:
            sec=max(1, int(duration//2))
    else:
        sec=5
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    try:
        # Use ffmpeg to extract frame at deterministic sec
        # scale to 640 width, keep aspect
        r=subprocess.run([FFMPEG_BIN, '-y', '-ss', str(sec), '-i', mp4_path, '-frames:v', '1', '-q:v', '2', '-vf', 'scale=640:-1', thumb_path], capture_output=True, timeout=15)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
        # fallback: try with -ss after -i
        if os.path.exists(thumb_path):
            try: os.remove(thumb_path)
            except: pass
        r=subprocess.run([FFMPEG_BIN, '-y', '-i', mp4_path, '-ss', str(sec), '-frames:v', '1', '-q:v', '2', '-vf', 'scale=640:-1', thumb_path], capture_output=True, timeout=15)
        if os.path.exists(thumb_path):
            return thumb_path
    except Exception:
        pass
    return None

def _dinguzplus_generate_previews(mid):
    if mid == '__intro__':
        intro_path=os.path.join(DINGUZPLUS_RESOURCES, "intro.mp4")
        if not os.path.exists(intro_path): return
        return _dinguzplus_generate_previews_for_path(mid, intro_path)
    safe=_dinguzplus_safe_id(mid)
    if not safe: return
    mpath=_dinguzplus_resolve_mp4(safe)
    if not mpath or not os.path.exists(mpath):
        return
    return _dinguzplus_generate_previews_for_path(safe, mpath)

def _dinguzplus_generate_previews_for_path(mid, mpath):
    """Generate all preview thumbnails for a given MP4 file under the mid's preview directory."""
    # get duration
    if mid == '__intro__':
        d=0
    else:
        d=_dinguzplus_get_metadata(mid).get('duration') if os.path.exists(_dinguzplus_metadata_path(mid) or "") else 0
    if not d:
        d=_dinguzplus_duration(mpath)
    if not d or d<=0:
        # fallback: try ffprobe duration
        try:
            import subprocess, json as js
            pr=subprocess.run([FFMPEG_BIN+"-probe","-v","error","-show_entries","format=duration","-of","json",mpath], capture_output=True, text=True, timeout=10)
            info=js.loads(pr.stdout) if pr.stdout else {}
            d=float(info.get("format",{}).get("duration",0))
        except Exception:
            d=0
    if not d or d<=0:
        d=60  # fallback 1 min
    # check existing previews: if all exist, skip
    preview_dir=_dinguzplus_ensure_preview_dir(safe)
    if not preview_dir:
        return
    # count expected previews
    expected=int(d//15)+1
    existing=len([f for f in os.listdir(preview_dir) if f.endswith('.webp')]) if os.path.isdir(preview_dir) else 0
    if existing>=expected:
        # check if all expected files exist
        all_exist=True
        for sec in range(0, int(d)+1, 15):
            p=_dinguzplus_preview_path(safe, sec)
            if not os.path.exists(p):
                all_exist=False
                break
        if all_exist:
            return
    # generate missing previews via ffmpeg
    try:
        import subprocess, shutil
        if not shutil.which("ffmpeg"):
            return
        for sec in range(0, int(d)+1, 15):
            out=_dinguzplus_preview_path(safe, sec)
            if os.path.exists(out):
                continue
            # ffmpeg generate thumbnail at sec
            try:
                subprocess.run(["ffmpeg","-y","-ss",str(sec),"-i",mpath,"-vframes","1","-vf","scale=320:-1","-q:v","5",out], timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except:
                pass
    except:
        pass

def _dinguzplus_preview_worker():
    global _DINGUZPLUS_PREVIEW_RUNNING
    if _DINGUZPLUS_PREVIEW_RUNNING:
        return
    _DINGUZPLUS_PREVIEW_RUNNING=True
    def _run():
        global _DINGUZPLUS_PREVIEW_RUNNING
        while True:
            try:
                mid=_DINGUZPLUS_PREVIEW_QUEUE.get(timeout=1)
            except:
                break
            try:
                _dinguzplus_generate_previews(mid)
            except:
                pass
            _DINGUZPLUS_PREVIEW_QUEUE.task_done()
        _DINGUZPLUS_PREVIEW_RUNNING=False
    t=_threading.Thread(target=_run, daemon=True)
    t.start()

def _dinguzplus_queue_preview(mid):
    safe=_dinguzplus_safe_id(mid)
    if not safe:
        return
    # avoid duplicate in queue
    if safe in list(_DINGUZPLUS_PREVIEW_QUEUE.queue):
        return
    _DINGUZPLUS_PREVIEW_QUEUE.put(safe)
    _dinguzplus_preview_worker()

def _dinguzplus_migrate_previews():
    # background migration for existing films/episodes without previews
    def _migrate():
        try:
            # films
            for fname in os.listdir(DINGUZPLUS_MOVIES) if os.path.isdir(DINGUZPLUS_MOVIES) else []:
                if not fname.lower().endswith('.mp4'): continue
                base=os.path.splitext(fname)[0]
                safe=_dinguzplus_safe_id(base)
                if not safe: continue
                # check if previews exist
                d=_dinguzplus_preview_dir(safe)
                if d and os.path.isdir(d) and any(f.endswith('.webp') for f in os.listdir(d)):
                    continue
                _dinguzplus_queue_preview(safe)
                import time; time.sleep(0.5)
            # series episodes
            for sid in os.listdir(DINGUZPLUS_SERIES) if os.path.isdir(DINGUZPLUS_SERIES) else []:
                sp=os.path.join(DINGUZPLUS_SERIES, sid)
                for sname in os.listdir(sp) if os.path.isdir(sp) else []:
                    sdir=os.path.join(sp, sname)
                    if not os.path.isdir(sdir) or not sname.startswith("season_"): continue
                    for ef in os.listdir(sdir):
                        if not ef.lower().endswith('.mp4'): continue
                        base=os.path.splitext(ef)[0]
                        safe=_dinguzplus_safe_id(base)
                        if not safe: continue
                        d=_dinguzplus_preview_dir(safe)
                        if d and os.path.isdir(d) and any(f.endswith('.webp') for f in os.listdir(d)):
                            continue
                        _dinguzplus_queue_preview(safe)
                        import time; time.sleep(0.5)
        except:
            pass
    t=_threading.Thread(target=_migrate, daemon=True)
    t.start()

def _dinguzplus_optimize_faststart(mpath):
    # check if moov is at beginning via qt-faststart check, if not, remux with -movflags faststart
    try:
        import subprocess
        # quick check: if file starts with ftyp and moov early, skip
        with open(mpath, 'rb') as f:
            header=f.read(4096)
            if b'moov' in header[:2048]:
                return  # already faststart
        # remux
        tmp=mpath+".faststart.tmp"
        subprocess.run(["ffmpeg","-y","-i",mpath,"-c","copy","-movflags","faststart",tmp], timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, mpath)
    except:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except: pass

def _dinguzplus_migrate_faststart():
    def _run():
        try:
            for fname in os.listdir(DINGUZPLUS_MOVIES) if os.path.isdir(DINGUZPLUS_MOVIES) else []:
                if not fname.lower().endswith('.mp4'): continue
                fpath=os.path.join(DINGUZPLUS_MOVIES, fname)
                _dinguzplus_optimize_faststart(fpath)
                import time; time.sleep(0.5)
            for sid in os.listdir(DINGUZPLUS_SERIES) if os.path.isdir(DINGUZPLUS_SERIES) else []:
                sp=os.path.join(DINGUZPLUS_SERIES, sid)
                for sname in os.listdir(sp) if os.path.isdir(sp) else []:
                    sdir=os.path.join(sp, sname)
                    if not os.path.isdir(sdir): continue
                    for ef in os.listdir(sdir):
                        if not ef.lower().endswith('.mp4'): continue
                        fpath=os.path.join(sdir, ef)
                        _dinguzplus_optimize_faststart(fpath)
                        import time; time.sleep(0.5)
        except: pass
    t=_threading.Thread(target=_run, daemon=True)
    t.start()

# run migration in background on startup
try:
    _dinguzplus_migrate_previews()
    _dinguzplus_migrate_faststart()
except:
    pass

def _dinguzplus_load_config():
    if os.path.exists(DINGUZPLUS_CONFIG):
        try:
            with open(DINGUZPLUS_CONFIG,'r') as f:
                return json.load(f)
        except: return {}
    return {}
def _dinguzplus_save_config(data):
    with open(DINGUZPLUS_CONFIG,'w') as f:
        json.dump(data,f,indent=2)
def _dinguzplus_is_premiered(meta):
    pa = meta.get('premiere_at')
    if not pa:
        return True
    try:
        # handle "30.08.2026 22:00" or ISO
        s=str(pa).strip()
        # try ISO first
        try:
            dt=datetime.datetime.fromisoformat(s.replace('Z',''))
        except:
            # try DD.MM.YYYY HH:MM
            dt=datetime.datetime.strptime(s, "%d.%m.%Y %H:%M")
        now=datetime.datetime.now()
        # normalize tz
        if dt.tzinfo is not None:
            now=now.replace(tzinfo=dt.tzinfo)
        return now >= dt
    except:
        return True
def _is_dinguzplus_admin():
    u=get_discord_user()
    return u and str(u.get('id'))==OWNER_DISCORD_ID
def _dinguzplus_require_admin(f):
    @wraps(f)
    def dec(*a,**kw):
        u=get_discord_user()
        if not u or str(u.get('id'))!=OWNER_DISCORD_ID:
            return jsonify({"error":"forbidden"}),403
        return f(*a,**kw)
    return dec

# --- DINGUZ+ HELPERS: MY LIST, RATINGS, VIEWS, HISTORY, CATEGORIES, SERIES ---
def _load_json_file(path, default):
    if os.path.exists(path):
        try:
            with open(path,'r') as f: return json.load(f)
        except: return default
    return default
def _save_json_file(path, data):
    with open(path,'w') as f: json.dump(data,f,indent=2)

def _dinguzplus_load_mylist(): return _load_json_file(DINGUZPLUS_MYLIST, {})
def _dinguzplus_save_mylist(d): _save_json_file(DINGUZPLUS_MYLIST, d)
def _dinguzplus_load_ratings(): return _load_json_file(DINGUZPLUS_RATINGS, {})
def _dinguzplus_save_ratings(d): _save_json_file(DINGUZPLUS_RATINGS, d)
def _dinguzplus_load_views(): return _load_json_file(DINGUZPLUS_VIEWS, {})
def _dinguzplus_save_views(d): _save_json_file(DINGUZPLUS_VIEWS, d)
def _dinguzplus_load_history(): return _load_json_file(DINGUZPLUS_HISTORY, {})
def _dinguzplus_save_history(d): _save_json_file(DINGUZPLUS_HISTORY, d)
def _dinguzplus_load_categories():
    d=_load_json_file(DINGUZPLUS_CATEGORIES, {"categories":["Filmy","Seriale","Dinguz Originals","Nowości","Premiery"]})
    if "categories" not in d: d={"categories":["Filmy"]}
    return d
def _dinguzplus_save_categories(d): _save_json_file(DINGUZPLUS_CATEGORIES, d)
def _dinguzplus_load_series():
    # series are directories under DINGUZPLUS_SERIES, each with meta.json
    res=[]
    if not os.path.isdir(DINGUZPLUS_SERIES): return res
    for sid in os.listdir(DINGUZPLUS_SERIES):
        sp=os.path.join(DINGUZPLUS_SERIES, sid)
        if not os.path.isdir(sp): continue
        meta_path=os.path.join(sp,"meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path,'r') as f: meta=json.load(f)
            except: meta={}
        else: meta={}
        # count seasons/episodes
        seasons=[]
        if os.path.isdir(sp):
            for sname in os.listdir(sp):
                sdir=os.path.join(sp,sname)
                if not os.path.isdir(sdir) or not sname.startswith("season_"): continue
                eps=[]
                # collect all episode JSONs, even if MP4 not yet uploaded
                for ef in os.listdir(sdir):
                    if ef.lower().endswith('.json'):
                        base=os.path.splitext(ef)[0]
                        # skip if base is season meta (season itself doesn't have json, only episodes)
                        ep_meta_path=os.path.join(sdir, ef)
                        try:
                            with open(ep_meta_path,'r') as f: ep_meta=json.load(f)
                        except: ep_meta={}
                        # check if corresponding MP4 exists
                        mp4_exists=os.path.exists(os.path.join(sdir, base+".mp4"))
                        eps.append({"id": base, "file": base+".mp4" if mp4_exists else None, "has_file": mp4_exists, "meta": ep_meta})
                # also handle legacy: if JSON missing but MP4 exists
                for ef in os.listdir(sdir):
                    if ef.lower().endswith('.mp4'):
                        base=os.path.splitext(ef)[0]
                        if any(e['id']==base for e in eps):
                            continue
                        ep_meta_path=os.path.join(sdir, base+".json")
                        if os.path.exists(ep_meta_path):
                            try:
                                with open(ep_meta_path,'r') as f: ep_meta=json.load(f)
                            except: ep_meta={}
                        else: ep_meta={}
                        eps.append({"id": base, "file": ef, "has_file": True, "meta": ep_meta})
                eps.sort(key=lambda x: x['meta'].get('order',999))
                seasons.append({"id": sname, "episodes": eps, "meta": meta})
            seasons.sort(key=lambda x: x['id'])
        res.append({"id": sid, "meta": meta, "seasons": seasons})
    return res

def _dinguzplus_get_series_path(sid):
    safe=_dinguzplus_safe_id(sid)
    if not safe: return None
    return os.path.join(DINGUZPLUS_SERIES, safe)

# PAGES
@app.route("/dinguzplus")
@requires_authorization
def dinguzplus_page():
    user=get_discord_user()
    # Pre-render movie sections server-side as fallback in case JS fails
    movies=_dinguzplus_list_movies()
    premiered=[m for m in movies if m.get('is_premiered')]
    by_section={}
    for m in premiered:
        sec=m.get('section') or 'Filmy'
        by_section.setdefault(sec, []).append(m)
    return render_template("dinguzplus.html", discord_user=user, server_movies=premiered, server_by_section=by_section)
# PAGES_OLD
@app.route("/dinguzplus.html")
@requires_authorization
def dinguzplus_page_html():
    return redirect(url_for("dinguzplus_page"), code=301)
    return render_template("dinguzplus.html", discord_user=user)

@app.route("/dinguzplus.html")
def dinguzplus_html_redirect():
    return redirect(url_for("dinguzplus_page"), code=301)

@app.route("/dinguzplus/watch/<movie_id>")
@requires_authorization
def dinguzplus_watch(movie_id):
    user=get_discord_user()
    safe=_dinguzplus_safe_id(movie_id)
    if not safe:
        return render_template("dinguzplus_watch_notfound.html", movie_id=movie_id),404
    mpath=_dinguzplus_movie_path(safe)
    meta=None
    # try film first
    if mpath and os.path.exists(mpath):
        meta=_dinguzplus_get_metadata(safe)
    else:
        # try series episode
        found=False
        for sid in os.listdir(DINGUZPLUS_SERIES) if os.path.isdir(DINGUZPLUS_SERIES) else []:
            sp=os.path.join(DINGUZPLUS_SERIES, sid)
            for sname in os.listdir(sp) if os.path.isdir(sp) else []:
                sdir=os.path.join(sp, sname)
                if not os.path.isdir(sdir) or not sname.startswith("season_"): continue
                cand=os.path.join(sdir, f"{safe}.mp4")
                cand_json=os.path.join(sdir, f"{safe}.json")
                if os.path.exists(cand) or os.path.exists(cand_json):
                    mpath=cand if os.path.exists(cand) else None
                    if os.path.exists(cand_json):
                        try:
                            with open(cand_json,'r') as f: meta=json.load(f)
                        except: meta={}
                    else:
                        meta={}
                    # ensure episode meta has required fields
                    if 'age_restriction' not in meta:
                        meta['age_restriction']=meta.get('age',16)
                    if 'movie_title' not in meta:
                        meta['movie_title']=meta.get('title') or _dinguzplus_pretty_name(safe)
                    found=True
                    break
            if found: break
        # try composite id: {sid}_season_{n}_{ep_id}
        if not found:
            mm=__import__('re').match(r'^(.+?)_season_(\d+?)_(.+)$', safe)
            if mm:
                csid, csn, cep = mm.group(1), mm.group(2), mm.group(3)
                csp=os.path.join(DINGUZPLUS_SERIES, csid, f"season_{csn}")
                if os.path.isdir(csp):
                    cand3=os.path.join(csp, f"{cep}.mp4")
                    cand3_json=os.path.join(csp, f"{cep}.json")
                    if os.path.exists(cand3):
                        mpath=cand3
                        if os.path.exists(cand3_json):
                            try:
                                with open(cand3_json,'r') as f: meta=json.load(f)
                            except: meta={}
                        else: meta={}
                        if 'age_restriction' not in meta:
                            meta['age_restriction']=meta.get('age',16)
                        if 'movie_title' not in meta:
                            meta['movie_title']=meta.get('title') or _dinguzplus_pretty_name(cep)
                        found=True
        if not found:
            return render_template("dinguzplus_watch_notfound.html", movie_id=movie_id),404
        if meta is None:
            meta={}
    if not meta:
        meta={}
    # ensure content_tags exists
    if 'content_tags' not in meta:
        meta['content_tags']=[]
    title = meta.get('movie_title') or meta.get('title') or _dinguzplus_pretty_name(safe)
    is_prem = _dinguzplus_is_premiered(meta)
    premiere_at = meta.get('premiere_at')
    # check profile selected - if no profiles exist, frontend will handle, but allow watch
    return render_template("dinguzplus_watch.html", discord_user=user, movie_id=safe, pretty=title, age=meta.get('age_restriction', meta.get('age',16)), meta=meta, premiere_at=premiere_at, is_premiered=is_prem)

# STREAMS
@app.route("/dinguzplus/stream/<movie_id>")
@requires_authorization
def dinguzplus_stream(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe:
        return "Not found",404
    mpath=_dinguzplus_movie_path(safe)
    meta=None
    if mpath and os.path.exists(mpath):
        meta=_dinguzplus_get_metadata(safe)
    else:
        # try series episode
        for sid in os.listdir(DINGUZPLUS_SERIES) if os.path.isdir(DINGUZPLUS_SERIES) else []:
            sp=os.path.join(DINGUZPLUS_SERIES, sid)
            for sname in os.listdir(sp) if os.path.isdir(sp) else []:
                sdir=os.path.join(sp, sname)
                if not os.path.isdir(sdir) or not sname.startswith("season_"): continue
                cand=os.path.join(sdir, f"{safe}.mp4")
                cand_json=os.path.join(sdir, f"{safe}.json")
                if os.path.exists(cand) or os.path.exists(cand_json):
                    mpath=cand if os.path.exists(cand) else None
                    if os.path.exists(cand_json):
                        try:
                            with open(cand_json,'r') as f: meta=json.load(f)
                        except: meta={}
                    else:
                        meta={}
                    break
            if mpath: break
        # try composite id: {sid}_season_{n}_{ep_id}
        if not mpath or not os.path.exists(mpath):
            m=__import__('re').match(r'^(.+?)_season_(\d+?)_(.+)$', safe)
            if m:
                csid, csn, cep = m.group(1), m.group(2), m.group(3)
                cand2=os.path.join(DINGUZPLUS_SERIES, csid, f"season_{csn}", f"{cep}.mp4")
                cand2_json=os.path.join(DINGUZPLUS_SERIES, csid, f"season_{csn}", f"{cep}.json")
                if os.path.exists(cand2):
                    mpath=cand2
                    if os.path.exists(cand2_json):
                        try:
                            with open(cand2_json,'r') as f: meta=json.load(f)
                        except: meta={}
                    else: meta={}
        if not mpath or not os.path.exists(mpath):
            return "Not found",404
        if meta is None:
            meta={}
        if 'age_restriction' not in meta and 'age' in meta:
            meta['age_restriction']=meta['age']
    if meta is None:
        meta=_dinguzplus_get_metadata(safe)
    # premiere check - admin bypass
    if not _dinguzplus_is_premiered(meta) and not _is_dinguzplus_admin():
        return jsonify({"error":"Premiera wkrótce"}),403
    # active profile check
    if not _dinguzplus_get_active_profile() and not _is_dinguzplus_admin():
        return jsonify({"error":"Wybierz profil"}),403
    if not mpath or not os.path.exists(mpath):
        return "Not found",404
    return _dinguzplus_stream_file(mpath, request)

def _dinguzplus_stream_file(mpath, request):
    """Stream a video file with full HTTP Range support, ETag, HEAD handling, and proxy-friendly headers.
    Returns a Response object. mpath must be an absolute path to an existing file.
    """
    file_size=os.path.getsize(mpath)
    # ETag for cache validation (weak - based on size + mtime)
    try:
        mtime=int(os.path.getmtime(mpath))
        etag=f'W/"x{file_size:x}-m{mtime:x}"'
        last_modified=datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    except Exception:
        etag=None
        last_modified=None
    # Always advertise Range support and disable proxy buffering
    base_headers={
        'Accept-Ranges':'bytes',
        'Content-Type':'video/mp4',
        'Cache-Control':'public, max-age=3600',
        'X-Accel-Buffering':'no',  # tell nginx not to buffer Range responses
        'Access-Control-Allow-Origin':'*',
        'Access-Control-Expose-Headers':'Accept-Ranges,Content-Length,Content-Range',
        'Vary':'Range',
    }
    if etag:
        base_headers['ETag']=etag
        base_headers['Last-Modified']=last_modified
    # Handle If-Range - if ETag doesn't match, ignore Range and send full file (also handle whitespace)
    if_range=request.headers.get('If-Range')
    range_header=request.headers.get('Range')
    if range_header:
        range_header=range_header.strip()
    if if_range:
        if_range=if_range.strip()
    if range_header and if_range and etag and if_range != etag:
        # also handle weak validator without W/ prefix
        if if_range.strip().lstrip('W/').strip('"') != etag.strip().lstrip('W/').strip('"'):
            range_header=None
    if range_header:
        m=re.match(r'bytes=(\d*)-(\d*)\s*$', range_header)
        if m:
            s,e=m.groups()
            if s=='' and e!='':
                # suffix range: last N bytes
                try:
                    length=int(e)
                except:
                    return Response(status=416, headers={'Content-Range':f'bytes */{file_size}','Accept-Ranges':'bytes'})
                if length<=0:
                    return Response(status=416, headers={'Content-Range':f'bytes */{file_size}','Accept-Ranges':'bytes'})
                start=max(0, file_size-length)
                end=file_size-1
            else:
                try:
                    start=int(s) if s else 0
                    end=int(e) if e else file_size-1
                except:
                    return Response(status=416, headers={'Content-Range':f'bytes */{file_size}','Accept-Ranges':'bytes'})
                # RFC 7233: if start >= file_size -> 416
                if start >= file_size:
                    return Response(status=416, headers={'Content-Range':f'bytes */{file_size}','Accept-Ranges':'bytes'})
                end=min(end, file_size-1)
                if end < start:
                    return Response(status=416, headers={'Content-Range':f'bytes */{file_size}','Accept-Ranges':'bytes'})
            length=end-start+1
            if length<=0 or start>=file_size:
                return Response(status=416, headers={'Content-Range':f'bytes */{file_size}','Accept-Ranges':'bytes'})
            headers=dict(base_headers)
            headers['Content-Range']=f'bytes {start}-{end}/{file_size}'
            headers['Content-Length']=str(length)
            # HEAD: headers only
            if request.method=='HEAD':
                return Response(status=206, headers=headers)
            # Stream the byte range using a generator
            def generate_range(path, offset, length, block_size=1024*256):
                with open(path, 'rb') as f:
                    f.seek(offset)
                    remaining=length
                    while remaining>0:
                        chunk=f.read(min(block_size, remaining))
                        if not chunk:
                            break
                        remaining-=len(chunk)
                        yield chunk
            return Response(generate_range(mpath, start, length), status=206, headers=headers, mimetype='video/mp4')
    # No Range - send full file
    headers=dict(base_headers)
    headers['Content-Length']=str(file_size)
    if request.method=='HEAD':
        return Response(status=200, headers=headers)
    # Stream the full file
    def generate_full(path, block_size=1024*256):
        with open(path, 'rb') as f:
            while True:
                chunk=f.read(block_size)
                if not chunk:
                    break
                yield chunk
    return Response(generate_full(mpath), status=200, headers=headers, mimetype='video/mp4')

@app.route("/dinguzplus/stream_intro")
@requires_authorization
def dinguzplus_stream_intro():
    p=os.path.join(DINGUZPLUS_RESOURCES, "intro.mp4")
    if not os.path.exists(p):
        return "No intro",404
    return _dinguzplus_stream_file(p, request)

@app.route("/dinguzplus/poster/<movie_id>")
def dinguzplus_poster(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe:
        return "Not found",404
    for ext in ['.jpg','.jpeg','.png','.webp']:
        pp=os.path.join(DINGUZPLUS_RESOURCES, safe+ext)
        if os.path.exists(pp):
            # guess mimetype
            mt='image/jpeg' if ext in ['.jpg','.jpeg'] else 'image/png' if ext=='.png' else 'image/webp'
            return send_file(pp, mimetype=mt)
    return "No poster",404

@app.route("/dinguzplus/thumbnail/<movie_id>")
def dinguzplus_thumbnail(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe:
        return "Not found",404
    if not _dinguzplus_movie_path(safe):
        return "No thumbnail",404
    p=_dinguzplus_find_thumb(safe, vertical=False)
    if not p or not os.path.exists(p):
        return "No thumbnail",404
    ext=os.path.splitext(p)[1].lower()
    mt='image/png' if ext=='.png' else 'image/jpeg' if ext in ('.jpg','.jpeg') else 'image/webp' if ext=='.webp' else 'image/png'
    resp=send_file(p, mimetype=mt)
    # allow cache but bust via ?v, also set ETag via mtime
    resp.headers['Cache-Control']='public, max-age=3600'
    return resp

@app.route("/dinguzplus/thumbnail_vertical/<movie_id>")
def dinguzplus_thumbnail_vertical(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe:
        return "Not found",404
    if not _dinguzplus_movie_path(safe):
        return "No thumbnail",404
    p=_dinguzplus_find_thumb(safe, vertical=True)
    if not p or not os.path.exists(p):
        # fallback to horizontal
        ph=_dinguzplus_find_thumb(safe, vertical=False)
        if ph and os.path.exists(ph):
            ext=os.path.splitext(ph)[1].lower()
            mt='image/png' if ext=='.png' else 'image/jpeg' if ext in ('.jpg','.jpeg') else 'image/webp' if ext=='.webp' else 'image/png'
            resp=send_file(ph, mimetype=mt)
            resp.headers['Cache-Control']='public, max-age=3600'
            return resp
        return "No thumbnail",404
    ext=os.path.splitext(p)[1].lower()
    mt='image/png' if ext=='.png' else 'image/jpeg' if ext in ('.jpg','.jpeg') else 'image/webp' if ext=='.webp' else 'image/png'
    resp=send_file(p, mimetype=mt)
    resp.headers['Cache-Control']='public, max-age=3600'
    return resp

@app.route("/dinguzplus/preview/<movie_id>")
@requires_authorization
def dinguzplus_preview(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe:
        return "Not found",404
    # check movie exists (film or episode, with composite ID support)
    mpath=None
    if _dinguzplus_movie_path(safe):
        pass  # film
    else:
        # also check series episodes - composite ID
        cm=__import__('re').match(r'^(.+?)_season_(\d+?)_(.+)$', safe)
        if cm:
            csid, csn, cep = cm.group(1), cm.group(2), cm.group(3)
            mpath=os.path.join(DINGUZPLUS_SERIES, csid, f"season_{csn}", f"{cep}.mp4")
            if not os.path.exists(mpath):
                return "Not found",404
        else:
            # try simple id in series
            found=False
            for sid in os.listdir(DINGUZPLUS_SERIES) if os.path.isdir(DINGUZPLUS_SERIES) else []:
                sp=os.path.join(DINGUZPLUS_SERIES, sid)
                for sname in os.listdir(sp) if os.path.isdir(sp) else []:
                    sdir=os.path.join(sp, sname)
                    if not os.path.isdir(sdir): continue
                    if os.path.exists(os.path.join(sdir, safe+".mp4")) or os.path.exists(os.path.join(sdir, safe+".json")):
                        found=True
                        break
            if not found:
                return "Not found",404
    # get nearest 15s preview
    try:
        t=float(request.args.get('t') or request.args.get('sec') or request.args.get('time') or 0)
    except:
        t=0
    # snap to nearest 15
    nearest=int(round(t/15)*15)
    p=_dinguzplus_preview_path(safe, nearest)
    if not p or not os.path.exists(p):
        # try to find closest existing
        preview_dir=_dinguzplus_preview_dir(safe)
        if preview_dir and os.path.isdir(preview_dir):
            files=[f for f in os.listdir(preview_dir) if f.endswith('.webp')]
            if files:
                # find closest
                best=None; best_diff=None
                for f in files:
                    try:
                        sec=int(os.path.splitext(f)[0])
                    except: continue
                    diff=abs(sec-nearest)
                    if best_diff is None or diff<best_diff:
                        best_diff=diff; best=f
                if best:
                    p=os.path.join(preview_dir, best)
        if not p or not os.path.exists(p):
            # auto-generate preview from MP4 (film or series episode)
            if not _dinguzplus_generate_preview(safe, nearest):
                return "Not found",404
            p=_dinguzplus_preview_path(safe, nearest)
            if not p or not os.path.exists(p):
                return "Not found",404
    # cache headers
    resp=send_file(p, mimetype="image/webp")
    resp.headers['Cache-Control']='public, max-age=86400'
    return resp

@app.route("/dinguzplus/preview_intro")
@requires_authorization
def dinguzplus_preview_intro():
    """Return preview thumbnail for the intro movie at given sec."""
    p=os.path.join(DINGUZPLUS_RESOURCES, "intro.mp4")
    if not os.path.exists(p):
        return "Not found",404
    try:
        t=float(request.args.get('t') or request.args.get('sec') or request.args.get('time') or 0)
    except Exception:
        t=0
    nearest=int(round(t/15)*15)
    pp=_dinguzplus_preview_path('__intro__', nearest)
    if not pp or not os.path.exists(pp):
        # try closest existing
        preview_dir=_dinguzplus_preview_dir('__intro__')
        if preview_dir and os.path.isdir(preview_dir):
            files=[f for f in os.listdir(preview_dir) if f.endswith('.webp')]
            if files:
                best=None; best_diff=None
                for f in files:
                    try:
                        sec=int(os.path.splitext(f)[0])
                    except Exception:
                        continue
                    diff=abs(sec-nearest)
                    if best_diff is None or diff<best_diff:
                        best_diff=diff; best=f
                if best:
                    pp=os.path.join(preview_dir, best)
        if not pp or not os.path.exists(pp):
            if not _dinguzplus_generate_preview('__intro__', nearest):
                return "Not found",404
            pp=_dinguzplus_preview_path('__intro__', nearest)
            if not pp or not os.path.exists(pp):
                return "Not found",404
    resp=send_file(pp, mimetype="image/webp")
    resp.headers['Cache-Control']='public, max-age=86400'
    return resp

# also alias for legacy poster to check thumbnails
@app.route("/dinguzplus/poster_thumb/<movie_id>")
def dinguzplus_poster_thumb(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe:
        return "Not found",404
    p=os.path.join(DINGUZPLUS_THUMBNAILS, safe+".png")
    if os.path.exists(p):
        return send_file(p, mimetype="image/png")
    return "No thumbnail",404

# API: movies
@app.route("/api/dinguzplus/movies")
@requires_authorization
def api_dinguzplus_movies():
    movies=_dinguzplus_list_movies()
    return jsonify(movies)

# API: unified library (movies + series) for Home
@app.route("/api/dinguzplus/library")
@requires_authorization
def api_dinguzplus_library():
    items=[]
    # movies
    for m in _dinguzplus_list_movies():
        items.append(m)
    # series
    try:
        series=_dinguzplus_load_series()
    except Exception:
        series=[]
    for s in series:
        sid=s.get('id')
        sm=s.get('meta') or {}
        # find season/episode counts and a representative thumbnail
        seasons_data=s.get('seasons') or []
        total_eps=sum(len(se.get('episodes') or []) for se in seasons_data)
        # poster: prefer DINGUZPLUS_THUMBNAILS/series/<sid>/thumb*
        poster=None
        poster_v=None
        thumb_dir=os.path.join(DINGUZPLUS_THUMBNAILS, "series", sid)
        if os.path.isdir(thumb_dir):
            for fn in os.listdir(thumb_dir):
                low=fn.lower()
                if low.startswith("thumb.") and not low.startswith("thumb_vertical") and poster is None:
                    poster=f"/dinguzplus/series_thumb_file/{sid}/{fn}"
                if low.startswith("thumb_vertical") and poster_v is None:
                    poster_v=f"/dinguzplus/series_thumb_file/{sid}/{fn}"
        # trailer
        has_trailer=os.path.exists(os.path.join(DINGUZPLUS_TRAILERS, "series", sid, "trailer.mp4"))
        cats=sm.get('categories') or ['Seriale']
        if isinstance(cats, str): cats=[cats]
        # first aired season/episode: latest uploaded
        first_season=seasons_data[0]['id'] if seasons_data else None
        first_ep=None
        if first_season and seasons_data[0].get('episodes'):
            first_ep=seasons_data[0]['episodes'][0].get('id')
        # use series premiere meta if set, else today
        premiere_at=sm.get('premiere_at')
        is_prem=True
        if premiere_at:
            is_prem=_dinguzplus_is_premiered({'premiere_at': premiere_at})
        try:
            # newest episode mtime as is_new
            is_new=False
            for se in seasons_data:
                for ep in se.get('episodes') or []:
                    if not ep.get('has_file'): continue
                    ep_json=os.path.join(DINGUZPLUS_SERIES, sid, se['id'], ep['id']+'.json')
                    if os.path.exists(ep_json):
                        mt=os.path.getmtime(ep_json)
                        if (datetime.datetime.now().timestamp()-mt) < 3*24*3600:
                            is_new=True; break
                if is_new: break
        except: is_new=False
        items.append({
            "id": sid,
            "is_series": True,
            "type": "series",
            "pretty": sm.get('title', sid),
            "movie_title": sm.get('title', sid),
            "filename": None,
            "file_name": None,
            "size_mb": 0,
            "duration": 0,
            "age": int(sm.get('age', sm.get('age_restriction', 16)) or 16),
            "poster": poster,
            "poster_vertical": poster_v,
            "premiere_at": premiere_at,
            "is_premiered": is_prem,
            "section": sm.get('section') or (cats[0] if cats else 'Seriale'),
            "categories": cats,
            "order": int(sm.get('order', 999) or 999),
            "description": sm.get('description',''),
            "has_trailer": has_trailer,
            "subtitles": [],
            "is_new": is_new,
            "content_tags": sm.get('content_tags', []),
            "seasons": len(seasons_data),
            "episodes": total_eps,
            "first_season": first_season,
            "first_episode": first_ep,
        })
    # sort: premiered by order ASC then created DESC; upcoming at end
    return jsonify(items)

# API: series asset (thumbnail/trailer etc) static file passthrough
@app.route("/dinguzplus/series_thumb_file/<sid>/<path:filename>")
@requires_authorization
def dinguzplus_series_thumb_file(sid, filename):
    safe=_dinguzplus_safe_id(sid)
    if not safe: return "Not found",404
    safe_name=secure_filename(filename)
    p=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe, safe_name)
    if not os.path.exists(p):
        return "Not found",404
    ext=os.path.splitext(safe_name)[1].lower()
    mt="image/png"
    if ext in ('.jpg','.jpeg'): mt="image/jpeg"
    elif ext=='.webp': mt="image/webp"
    return send_file(p, mimetype=mt)

@app.route("/api/dinguzplus/movie/<movie_id>")
@requires_authorization
def api_dinguzplus_movie(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe:
        return jsonify({"error":"invalid"}),400
    mpath=_dinguzplus_movie_path(safe)
    if not mpath:
        return jsonify({"error":"not found"}),404
    meta=_dinguzplus_get_metadata(safe)
    # find poster (with mtime for cache bust -> hero sync)
    poster=None
    poster_mtime=0
    thumb_p=_dinguzplus_find_thumb(safe, vertical=False)
    if thumb_p:
        poster=f"/dinguzplus/thumbnail/{safe}?v={_dinguzplus_thumb_mtime(safe, False)}"
        poster_mtime=_dinguzplus_thumb_mtime(safe, False)
    else:
        for ext in ['.jpg','.jpeg','.png','.webp']:
            pp=os.path.join(DINGUZPLUS_RESOURCES, safe+ext)
            if os.path.exists(pp):
                try: poster_mtime=int(os.path.getmtime(pp))
                except: poster_mtime=0
                poster=f"/dinguzplus/poster/{safe}?v={poster_mtime}" if poster_mtime else f"/dinguzplus/poster/{safe}"
                break
    poster_vertical=None
    vert_p=_dinguzplus_find_thumb(safe, vertical=True)
    if vert_p:
        poster_vertical=f"/dinguzplus/thumbnail_vertical/{safe}?v={_dinguzplus_thumb_mtime(safe, True)}"
    elif thumb_p:
        poster_vertical=poster
    has_trailer=os.path.exists(os.path.join(DINGUZPLUS_TRAILERS, safe+".mp4")) or os.path.exists(os.path.join(DINGUZPLUS_TRAILERS, safe+"_trailer.mp4"))
    subs=[]
    sub_dir=os.path.join(DINGUZPLUS_SUBTITLES, safe)
    if os.path.isdir(sub_dir):
        for sf in os.listdir(sub_dir):
            if sf.lower().endswith('.vtt'):
                subs.append(os.path.splitext(sf)[0])
    cats=meta.get('categories') or [meta.get('section','Filmy')]
    if isinstance(cats, str): cats=[cats]
    try:
        is_new=(datetime.datetime.now().timestamp() - os.path.getmtime(mpath)) < 3*24*3600
    except: is_new=False
    return jsonify({
        "id": safe,
        "pretty": meta.get('movie_title', _dinguzplus_pretty_name(safe)),
        "movie_title": meta.get('movie_title', _dinguzplus_pretty_name(safe)),
        "filename": os.path.basename(mpath),
        "file_name": meta.get('file_name', os.path.basename(mpath)),
        "size_mb": round(os.path.getsize(mpath)/(1024*1024),2),
        "duration": meta.get('duration',0),
        "age": meta.get('age_restriction',16),
        "poster": poster,
        "poster_vertical": poster_vertical,
        "poster_mtime": poster_mtime,
        "premiere_at": meta.get('premiere_at'),
        "is_premiered": _dinguzplus_is_premiered(meta),
        "section": meta.get('section','Filmy'),
        "categories": cats,
        "order": int(meta.get('order',999)),
        "description": meta.get('description',''),
        "has_trailer": has_trailer,
        "subtitles": subs,
        "is_new": is_new,
        "content_tags": meta.get('content_tags',[])
    })

@app.route("/api/dinguzplus/config", methods=["GET"])
@requires_authorization
def api_dinguzplus_config_public():
    cfg=_dinguzplus_load_config()
    return jsonify(cfg)

# API: profiles
@app.route("/api/dinguzplus/profiles", methods=["GET"])
@requires_authorization
def api_dinguzplus_profiles_list():
    user=get_discord_user()
    did=str(user.get('id'))
    data=_dinguzplus_load_profiles()
    profiles=data.get(did,[])
    active=session.get('dinguzplus_profile_id')
    return jsonify({"profiles": profiles, "active": active})

@app.route("/api/dinguzplus/profiles", methods=["POST"])
@requires_authorization
def api_dinguzplus_profiles_create():
    user=get_discord_user()
    did=str(user.get('id'))
    j=request.json or {}
    name=(j.get('name') or '').strip()
    if not name or len(name)<1 or len(name)>20:
        return jsonify({"error":"Nazwa 1-20 znaków"}),400
    if not re.match(r'^[a-zA-Z0-9 _-]+$', name):
        return jsonify({"error":"Dozwolone znaki a-z 0-9 _ -"}),400
    data=_dinguzplus_load_profiles()
    profiles=data.get(did,[])
    if len(profiles)>=5:
        return jsonify({"error":"Max 5 profili"}),400
    # check duplicate
    if any(p['name'].lower()==name.lower() for p in profiles):
        return jsonify({"error":"Profil o tej nazwie już istnieje"}),400
    pid=''.join(random.choices(string.ascii_lowercase+string.digits, k=8))
    colors=["#e94560","#22c55e","#06b6d4","#a855f7","#eab308","#f97316"]
    profile={"id":pid, "name": name, "color": random.choice(colors), "created": datetime.datetime.now().isoformat()}
    profiles.append(profile)
    data[did]=profiles
    _dinguzplus_save_profiles(data)
    return jsonify(profile)

@app.route("/api/dinguzplus/profiles/<profile_id>", methods=["DELETE"])
@requires_authorization
def api_dinguzplus_profiles_delete(profile_id):
    user=get_discord_user()
    did=str(user.get('id'))
    data=_dinguzplus_load_profiles()
    profiles=data.get(did,[])
    new=[p for p in profiles if p['id']!=profile_id]
    if len(new)==len(profiles):
        return jsonify({"error":"not found"}),404
    data[did]=new
    _dinguzplus_save_profiles(data)
    if session.get('dinguzplus_profile_id')==profile_id:
        session.pop('dinguzplus_profile_id',None)
    # also delete progress for this profile
    prog=_dinguzplus_load_progress()
    if did in prog and profile_id in prog[did]:
        del prog[did][profile_id]
        _dinguzplus_save_progress(prog)
    return jsonify({"ok":True})

@app.route("/api/dinguzplus/profiles/select", methods=["POST"])
@requires_authorization
def api_dinguzplus_profiles_select():
    user=get_discord_user()
    did=str(user.get('id'))
    j=request.json or {}
    pid=j.get('profile_id')
    if not pid:
        return jsonify({"error":"brak id"}),400
    data=_dinguzplus_load_profiles()
    profiles=data.get(did,[])
    if not any(p['id']==pid for p in profiles):
        return jsonify({"error":"not found"}),404
    session['dinguzplus_profile_id']=pid
    session.permanent=True
    return jsonify({"ok":True, "active":pid})

# API: progress
@app.route("/api/dinguzplus/progress", methods=["GET"])
@requires_authorization
def api_dinguzplus_progress_get():
    user=get_discord_user()
    did=str(user.get('id'))
    pid=_dinguzplus_get_active_profile()
    if not pid:
        return jsonify({"error":"no profile"}),400
    prog=_dinguzplus_load_progress()
    user_prog=prog.get(did,{}).get(pid,{})
    return jsonify(user_prog)

@app.route("/api/dinguzplus/progress/<movie_id>", methods=["GET"])
@requires_authorization
def api_dinguzplus_progress_one(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe:
        return jsonify({"error":"invalid"}),400
    user=get_discord_user()
    did=str(user.get('id'))
    pid=_dinguzplus_get_active_profile()
    if not pid:
        return jsonify({"error":"no profile"}),400
    prog=_dinguzplus_load_progress()
    data=prog.get(did,{}).get(pid,{}).get(safe)
    if not data:
        return jsonify({"position":0,"duration":0,"completed":False})
    return jsonify(data)

@app.route("/api/dinguzplus/progress", methods=["POST"])
@requires_authorization
def api_dinguzplus_progress_save():
    user=get_discord_user()
    did=str(user.get('id'))
    pid=_dinguzplus_get_active_profile()
    if not pid:
        return jsonify({"error":"no profile"}),400
    j=request.json or {}
    mid=_dinguzplus_safe_id(j.get('movie_id') or j.get('id') or '')
    if not mid:
        return jsonify({"error":"invalid movie"}),400
    # allow movies, series, and series episodes (composite)
    if not _dinguzplus_movie_path(mid):
        # check series
        is_valid=False
        try:
            series=_dinguzplus_load_series()
            if any(x['id']==mid for x in series):
                is_valid=True
            else:
                # check composite episode id
                m=re.match(r'^(.+?)_season_(\d+)_(.+)$', mid)
                if m:
                    csid, csn, cep = m.group(1), m.group(2), m.group(3)
                    cand=os.path.join(DINGUZPLUS_SERIES, csid, f"season_{csn}", f"{cep}.mp4")
                    if os.path.exists(cand):
                        is_valid=True
                    else:
                        # also check simple episode id in any series
                        for s in series:
                            for sea in s.get('seasons',[]):
                                if any(e['id']==mid and e['has_file'] for e in sea.get('episodes',[])):
                                    is_valid=True
                                    break
                            if is_valid: break
        except: pass
        if not is_valid:
            return jsonify({"error":"not found"}),404
    pos=float(j.get('position',0))
    dur=float(j.get('duration',0))
    completed=bool(j.get('completed', False))
    if pos<0: pos=0
    if dur<0: dur=0
    # if near end (>95%) mark completed
    if dur>0 and pos/dur>0.95:
        completed=True
    prog=_dinguzplus_load_progress()
    if did not in prog: prog[did]={}
    if pid not in prog[did]: prog[did][pid]={}
    prog[did][pid][mid]={"position": round(pos,1), "duration": round(dur,1), "completed": completed, "updated": datetime.datetime.now().isoformat()}
    _dinguzplus_save_progress(prog)
    return jsonify({"ok":True})

# ==========================================
#           DINGUZ+ ADMIN PANEL
# ==========================================
@app.route("/dinguzplus/admin")
@requires_authorization
def dinguzplus_admin():
    if not _is_dinguzplus_admin():
        return "Brak dostępu",403
    user=get_discord_user()
    return render_template("dinguzplus_admin.html", discord_user=user)

@app.route("/api/dinguzplus/admin/movies", methods=["GET"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_movies():
    movies=_dinguzplus_list_movies()
    cfg=_dinguzplus_load_config()
    return jsonify({"movies": movies, "hero_id": cfg.get("hero_id"), "config": cfg})

@app.route("/api/dinguzplus/admin/upload", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_upload():
    # multipart: file (mp4), thumbnail (png optional), fields
    if 'file' not in request.files:
        return jsonify({"error":"Brak pliku .mp4"}),400
    f=request.files['file']
    if not f.filename.lower().endswith('.mp4'):
        return jsonify({"error":"Dozwolone tylko .mp4"}),400
    orig_name=secure_filename(f.filename)
    base=os.path.splitext(orig_name)[0]
    safe=_dinguzplus_safe_id(base)
    if not safe:
        safe=''.join(random.choices(string.ascii_lowercase+string.digits, k=8))
    # ensure unique if exists
    mpath=os.path.join(DINGUZPLUS_MOVIES, safe+".mp4")
    if os.path.exists(mpath):
        safe=safe+"_"+''.join(random.choices(string.digits,k=3))
        mpath=os.path.join(DINGUZPLUS_MOVIES, safe+".mp4")
    f.save(mpath)
    # metadata
    movie_title=(request.form.get('movie_title') or '').strip() or _dinguzplus_pretty_name(safe)
    age=int(request.form.get('age_restriction') or 16)
    premiere_at=(request.form.get('premiere_at') or '').strip() or None
    # normalize premiere_at: frontend sends datetime-local in Europe/Warsaw -> store UTC ISO with Z
    if premiere_at:
        try:
            from zoneinfo import ZoneInfo
            # datetime-local from browser is Warsaw time
            dt=datetime.datetime.fromisoformat(premiere_at.replace('Z',''))
            if dt.tzinfo is None:
                dt=dt.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
            dt_utc=dt.astimezone(datetime.timezone.utc)
            premiere_at=dt_utc.isoformat().replace("+00:00","Z")
        except:
            try:
                from zoneinfo import ZoneInfo
                dt=datetime.datetime.strptime(premiere_at, "%d.%m.%Y %H:%M")
                dt=dt.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
                dt_utc=dt.astimezone(datetime.timezone.utc)
                premiere_at=dt_utc.isoformat().replace("+00:00","Z")
            except:
                premiere_at=None
    section=(request.form.get('section') or 'Filmy').strip() or 'Filmy'
    order_raw=request.form.get('order')
    try: order=int(order_raw) if order_raw else 999
    except: order=999
    description=(request.form.get('description') or '').strip()
    # content tags - filter by age
    raw_tags=request.form.get('content_tags') or request.form.get('tags') or ''
    try:
        if raw_tags.strip().startswith('['):
            import json as js2
            tags_list=js2.loads(raw_tags)
        else:
            tags_list=[t.strip() for t in raw_tags.split(',') if t.strip()]
    except:
        tags_list=[]
    allowed=_DINGUZPLUS_CONTENT_TAGS.get(int(age), [])
    tags_list=[t for t in tags_list if t in allowed]
    if int(age) in [3,7]:
        tags_list=[]
    meta={"age_restriction": age, "file_name": os.path.basename(mpath), "movie_title": movie_title, "premiere_at": premiere_at, "section": section, "categories": [section], "order": order, "description": description, "content_tags": tags_list}
    # duration
    d=_dinguzplus_duration(mpath)
    if d: meta['duration']=round(d,1)
    with open(_dinguzplus_metadata_path(safe),'w') as jf:
        json.dump(meta,jf,indent=2)
    # thumbnail if provided (custom + vertical, any image -> PNG canonical)
    if 'thumbnail' in request.files:
        thumb=request.files['thumbnail']
        if thumb.filename and thumb.filename.lower().endswith(('.png','.jpg','.jpeg','.webp')):
            _dinguzplus_save_thumb_file(thumb, os.path.join(DINGUZPLUS_THUMBNAILS, safe))
    if 'thumbnail_vertical' in request.files:
        vthumb=request.files['thumbnail_vertical']
        if vthumb.filename and vthumb.filename.lower().endswith(('.png','.jpg','.jpeg','.webp')):
            _dinguzplus_save_thumb_file(vthumb, os.path.join(DINGUZPLUS_THUMBNAILS, safe+"_vertical"))
    # queue preview generation in background
    try:
        _dinguzplus_queue_preview(safe)
    except: pass
    return jsonify({"ok":True, "id": safe, "meta": meta})

# --- CHUNKED UPLOAD ≤100MB ---
@app.route("/api/dinguzplus/admin/upload_init", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_upload_init():
    j=request.json or {}
    filename=secure_filename(j.get('filename') or '')
    size=int(j.get('size') or 0)
    total_chunks=int(j.get('totalChunks') or j.get('total_chunks') or 0)
    if not filename.lower().endswith('.mp4'):
        return jsonify({"error":"Dozwolone tylko .mp4"}),400
    if size <=0 or size > 50*1024*1024*1024:
        return jsonify({"error":"Nieprawidłowy rozmiar"}),400
    base=os.path.splitext(filename)[0]
    safe=_dinguzplus_safe_id(base)
    if not safe:
        safe=''.join(random.choices(string.ascii_lowercase+string.digits, k=8))
    # ensure unique
    mpath=os.path.join(DINGUZPLUS_MOVIES, safe+".mp4")
    if os.path.exists(mpath):
        safe=safe+"_"+''.join(random.choices(string.digits,k=3))
        mpath=os.path.join(DINGUZPLUS_MOVIES, safe+".mp4")
    upload_id=uuid.uuid4().hex
    tmp_dir=os.path.join(DINGUZPLUS_MOVIES, f".tmp_{upload_id}_{safe}")
    os.makedirs(tmp_dir, exist_ok=True)
    meta={"uploadId": upload_id, "safe": safe, "filename": filename, "size": size, "totalChunks": total_chunks, "tmp_dir": tmp_dir, "created": datetime.datetime.now().isoformat()}
    DINGUZPLUS_UPLOAD_SESSIONS[upload_id]=meta
    try:
        with open(os.path.join(tmp_dir, ".meta.json"),"w") as f: json.dump(meta,f)
    except: pass
    return jsonify({"uploadId": upload_id, "safe": safe})

@app.route("/api/dinguzplus/admin/upload_chunk", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_upload_chunk():
    upload_id=request.form.get('uploadId') or request.form.get('upload_id')
    chunk_index=request.form.get('chunkIndex') or request.form.get('chunk_index') or '0'
    try: chunk_index=int(chunk_index)
    except: chunk_index=0
    meta=DINGUZPLUS_UPLOAD_SESSIONS.get(upload_id)
    if not meta:
        # try load from disk
        for root, dirs, files in os.walk(DINGUZPLUS_MOVIES):
            for d in dirs:
                if d.startswith(f".tmp_{upload_id}_"):
                    try:
                        with open(os.path.join(root,d,".meta.json"),"r") as f:
                            meta=json.load(f)
                            DINGUZPLUS_UPLOAD_SESSIONS[upload_id]=meta
                            break
                    except: pass
    if not meta:
        return jsonify({"error":"Nieznany uploadId"}),404
    if 'file' not in request.files:
        return jsonify({"error":"Brak chunku"}),400
    chunk=request.files['file']
    # validate chunk size ≤100MB
    chunk.seek(0, os.SEEK_END)
    sz=chunk.tell()
    chunk.seek(0)
    if sz > 100*1024*1024:
        return jsonify({"error":"Chunk ≤100MB"}),400
    tmp_dir=meta['tmp_dir']
    if not os.path.isdir(tmp_dir):
        return jsonify({"error":"Sesja wygasła"}),404
    chunk_path=os.path.join(tmp_dir, f"chunk_{chunk_index:06d}")
    # write without loading to RAM (stream)
    with open(chunk_path+".tmp","wb") as out:
        for b in iter(lambda: chunk.read(8192), b""):
            out.write(b)
    os.replace(chunk_path+".tmp", chunk_path)
    return jsonify({"ok":True, "chunk": chunk_index})

@app.route("/api/dinguzplus/admin/upload_status", methods=["GET"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_upload_status():
    upload_id=request.args.get('uploadId') or request.args.get('upload_id')
    meta=DINGUZPLUS_UPLOAD_SESSIONS.get(upload_id)
    if not meta:
        return jsonify({"error":"not found"}),404
    tmp_dir=meta['tmp_dir']
    if not os.path.isdir(tmp_dir):
        return jsonify({"uploaded": []})
    chunks=[f for f in os.listdir(tmp_dir) if f.startswith("chunk_")]
    indices=sorted([int(f.split("_")[1].split(".")[0]) for f in chunks])
    return jsonify({"uploaded": indices, "total": meta.get('totalChunks')})

@app.route("/api/dinguzplus/admin/upload_complete", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_upload_complete():
    j=request.json or {}
    upload_id=j.get('uploadId') or j.get('upload_id')
    meta=DINGUZPLUS_UPLOAD_SESSIONS.get(upload_id)
    if not meta:
        # try disk
        for root, dirs, files in os.walk(DINGUZPLUS_MOVIES):
            for d in dirs:
                if d.startswith(f".tmp_{upload_id}_"):
                    try:
                        with open(os.path.join(root,d,".meta.json"),"r") as f:
                            meta=json.load(f)
                            DINGUZPLUS_UPLOAD_SESSIONS[upload_id]=meta
                            break
                    except: pass
    if not meta:
        return jsonify({"error":"Nieznany uploadId"}),404
    safe=meta['safe']
    tmp_dir=meta['tmp_dir']
    mpath=os.path.join(DINGUZPLUS_MOVIES, safe+".mp4")
    # collect chunks
    chunk_files=sorted([f for f in os.listdir(tmp_dir) if f.startswith("chunk_")])
    if not chunk_files:
        return jsonify({"error":"Brak chunków"}),400
    # validate consecutive
    try:
        indices=[int(f.split("_")[1]) for f in chunk_files]
        if indices != list(range(min(indices), max(indices)+1)):
            return jsonify({"error":"Dziury w chunkach"}),400
    except: pass
    # assemble without RAM (stream)
    tmp_final=mpath+".assemble.tmp"
    try:
        with open(tmp_final,"wb") as out:
            for fname in sorted(chunk_files):
                p=os.path.join(tmp_dir, fname)
                with open(p,"rb") as cf:
                    shutil.copyfileobj(cf, out, length=8192)
        # validate size
        got=os.path.getsize(tmp_final)
        if got != int(meta.get('size', got)):
            try: os.remove(tmp_final)
            except: pass
            return jsonify({"error":f"Rozmiar mismatch {got} vs {meta.get('size')}"}),400
        # validate is mp4 (simple check: file exists and >1KB)
        if got < 1024:
            try: os.remove(tmp_final)
            except: pass
            return jsonify({"error":"Plik uszkodzony"}),400
        os.replace(tmp_final, mpath)
        # cleanup
        try: shutil.rmtree(tmp_dir)
        except: pass
        DINGUZPLUS_UPLOAD_SESSIONS.pop(upload_id, None)
        # create metadata with provided fields
        movie_title=(j.get('movie_title') or '').strip() or _dinguzplus_pretty_name(safe)
        age=int(j.get('age_restriction') or 16)
        premiere_at=(j.get('premiere_at') or '').strip() or None
        if premiere_at:
            try:
                from zoneinfo import ZoneInfo
                dt=datetime.datetime.fromisoformat(premiere_at.replace('Z',''))
                if dt.tzinfo is None:
                    dt=dt.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
                dt_utc=dt.astimezone(datetime.timezone.utc)
                premiere_at=dt_utc.isoformat().replace("+00:00","Z")
            except:
                try:
                    from zoneinfo import ZoneInfo
                    dt=datetime.datetime.strptime(premiere_at, "%d.%m.%Y %H:%M")
                    dt=dt.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
                    dt_utc=dt.astimezone(datetime.timezone.utc)
                    premiere_at=dt_utc.isoformat().replace("+00:00","Z")
                except: premiere_at=None
        section=(j.get('section') or 'Filmy').strip() or 'Filmy'
        order=int(j.get('order') or 999)
        description=(j.get('description') or '').strip()
        cats=j.get('categories') or [section]
        if isinstance(cats, str): cats=[c.strip() for c in cats.split(',') if c.strip()]
        if not isinstance(cats, list): cats=[section]
        cats=[c.strip() for c in cats if c.strip()]
        if not cats: cats=[section]
        # content tags
        raw_tags=j.get('content_tags') or j.get('tags') or ''
        if isinstance(raw_tags, str):
            try:
                import json as js2
                if raw_tags.strip().startswith('['):
                    raw_tags=js2.loads(raw_tags)
                else:
                    raw_tags=[t.strip() for t in raw_tags.split(',') if t.strip()]
            except:
                raw_tags=[]
        elif not isinstance(raw_tags, list):
            raw_tags=[]
        allowed=_DINGUZPLUS_CONTENT_TAGS.get(int(age), [])
        raw_tags=[t for t in raw_tags if t in allowed]
        if int(age) in [3,7]:
            raw_tags=[]
        meta_json={"age_restriction": age, "file_name": os.path.basename(mpath), "movie_title": movie_title, "premiere_at": premiere_at, "section": section, "categories": cats, "order": order, "description": description, "trailer": None, "subtitles": [], "content_tags": raw_tags}
        d=_dinguzplus_duration(mpath)
        if d: meta_json['duration']=round(d,1)
        with open(_dinguzplus_metadata_path(safe),'w') as jf:
            json.dump(meta_json,jf,indent=2)
        # queue preview generation (existing films and new)
        try:
            _dinguzplus_queue_preview(safe)
        except: pass
        return jsonify({"ok":True, "id": safe, "meta": meta_json})
    except Exception as e:
        try:
            if os.path.exists(tmp_final): os.remove(tmp_final)
        except: pass
        return jsonify({"error": str(e)}),500

@app.route("/api/dinguzplus/admin/update/<movie_id>", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_update(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe or not _dinguzplus_movie_path(safe):
        return jsonify({"error":"not found"}),404
    # support both JSON and multipart
    if request.is_json:
        j=request.json
        meta=_dinguzplus_get_metadata(safe)
        if 'movie_title' in j: meta['movie_title']=str(j['movie_title']).strip() or meta['movie_title']
        if 'age_restriction' in j: meta['age_restriction']=int(j['age_restriction'])
        if 'premiere_at' in j:
            pa=j['premiere_at']
            if not pa: meta['premiere_at']=None
            else:
                try:
                    from zoneinfo import ZoneInfo
                    dt=datetime.datetime.fromisoformat(str(pa).replace('Z',''))
                    if dt.tzinfo is None:
                        dt=dt.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
                    dt_utc=dt.astimezone(datetime.timezone.utc)
                    meta['premiere_at']=dt_utc.isoformat().replace("+00:00","Z")
                except:
                    try:
                        from zoneinfo import ZoneInfo
                        dt=datetime.datetime.strptime(str(pa), "%d.%m.%Y %H:%M")
                        dt=dt.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
                        dt_utc=dt.astimezone(datetime.timezone.utc)
                        meta['premiere_at']=dt_utc.isoformat().replace("+00:00","Z")
                    except: meta['premiere_at']=None
        if 'section' in j: meta['section']=str(j['section']).strip() or 'Filmy'
        if 'categories' in j:
            cats=j.get('categories') or []
            if isinstance(cats, str): cats=[c.strip() for c in cats.split(',') if c.strip()]
            if not isinstance(cats, list): cats=[]
            if cats:
                # keep section as first selected for backwards compat
                meta['section']=cats[0]
                meta['categories']=cats
            else:
                meta['categories']=[meta.get('section','Filmy')]
        if 'order' in j:
            try: meta['order']=int(j['order'])
            except: pass
        if 'description' in j: meta['description']=str(j['description'])
        if 'content_tags' in j or 'tags' in j:
            raw=j.get('content_tags') or j.get('tags') or []
            if isinstance(raw, str):
                try:
                    import json as js2
                    if raw.strip().startswith('['):
                        raw=js2.loads(raw)
                    else:
                        raw=[t.strip() for t in raw.split(',') if t.strip()]
                except:
                    raw=[]
            if not isinstance(raw, list):
                raw=[]
            # filter by current age
            age_val=int(meta.get('age_restriction',16))
            allowed=_DINGUZPLUS_CONTENT_TAGS.get(age_val, [])
            if age_val in [3,7]:
                meta['content_tags']=[]
            else:
                meta['content_tags']=[t for t in raw if t in allowed]
        # if age changed, filter existing tags
        if 'age_restriction' in j:
            age_val=int(meta.get('age_restriction',16))
            allowed=_DINGUZPLUS_CONTENT_TAGS.get(age_val, [])
            if age_val in [3,7]:
                meta['content_tags']=[]
            else:
                meta['content_tags']=[t for t in meta.get('content_tags',[]) if t in allowed]
        with open(_dinguzplus_metadata_path(safe),'w') as f:
            json.dump(meta,f,indent=2)
        return jsonify({"ok":True, "meta": meta})
    else:
        # multipart with possible thumbnail
        meta=_dinguzplus_get_metadata(safe)
        if 'movie_title' in request.form: 
            v=request.form.get('movie_title','').strip()
            if v: meta['movie_title']=v
        if 'age_restriction' in request.form:
            try: meta['age_restriction']=int(request.form.get('age_restriction'))
            except: pass
        if 'premiere_at' in request.form:
            pa=request.form.get('premiere_at','').strip()
            if not pa: meta['premiere_at']=None
            else:
                try:
                    from zoneinfo import ZoneInfo
                    dt=datetime.datetime.fromisoformat(pa.replace('Z',''))
                    if dt.tzinfo is None:
                        dt=dt.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
                    dt_utc=dt.astimezone(datetime.timezone.utc)
                    meta['premiere_at']=dt_utc.isoformat().replace("+00:00","Z")
                except:
                    try:
                        from zoneinfo import ZoneInfo
                        dt=datetime.datetime.strptime(pa, "%d.%m.%Y %H:%M")
                        dt=dt.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
                        dt_utc=dt.astimezone(datetime.timezone.utc)
                        meta['premiere_at']=dt_utc.isoformat().replace("+00:00","Z")
                    except: meta['premiere_at']=None
        if 'section' in request.form: meta['section']=request.form.get('section','').strip() or 'Filmy'
        if 'categories' in request.form:
            cats=request.form.get('categories','').strip()
            cats_list=[c.strip() for c in cats.split(',') if c.strip()]
            if cats_list:
                meta['section']=cats_list[0]
                meta['categories']=cats_list
            else:
                meta['categories']=[meta.get('section','Filmy')]
        if 'order' in request.form:
            try: meta['order']=int(request.form.get('order'))
            except: pass
        if 'description' in request.form: meta['description']=request.form.get('description','').strip()
        if 'content_tags' in request.form or 'tags' in request.form:
            raw=request.form.get('content_tags') or request.form.get('tags') or ''
            try:
                import json as js2
                if raw.strip().startswith('['):
                    raw=js2.loads(raw)
                else:
                    raw=[t.strip() for t in raw.split(',') if t.strip()]
            except:
                raw=[]
            if not isinstance(raw, list):
                raw=[]
            age_val=int(meta.get('age_restriction',16))
            allowed=_DINGUZPLUS_CONTENT_TAGS.get(age_val, [])
            if age_val in [3,7]:
                meta['content_tags']=[]
            else:
                meta['content_tags']=[t for t in raw if t in allowed]
        # if age changed, filter existing tags
        if 'age_restriction' in request.form:
            age_val=int(meta.get('age_restriction',16))
            allowed=_DINGUZPLUS_CONTENT_TAGS.get(age_val, [])
            if age_val in [3,7]:
                meta['content_tags']=[]
            else:
                meta['content_tags']=[t for t in meta.get('content_tags',[]) if t in allowed]
        with open(_dinguzplus_metadata_path(safe),'w') as f:
            json.dump(meta,f,indent=2)
        if 'thumbnail' in request.files:
            thumb=request.files['thumbnail']
            if thumb.filename and thumb.filename.lower().endswith(('.png','.jpg','.jpeg','.webp')):
                _dinguzplus_save_thumb_file(thumb, os.path.join(DINGUZPLUS_THUMBNAILS, safe))
        if 'thumbnail_vertical' in request.files:
            vthumb=request.files['thumbnail_vertical']
            if vthumb.filename and vthumb.filename.lower().endswith(('.png','.jpg','.jpeg','.webp')):
                _dinguzplus_save_thumb_file(vthumb, os.path.join(DINGUZPLUS_THUMBNAILS, safe+"_vertical"))
        # handle thumbnail removal (any ext)
        if request.form.get('remove_thumbnail')=='1':
            for ext in _DINGUZPLUS_THUMB_EXTS:
                try: os.remove(os.path.join(DINGUZPLUS_THUMBNAILS, safe+ext))
                except: pass
        if request.form.get('remove_thumbnail_vertical')=='1':
            for ext in _DINGUZPLUS_THUMB_EXTS:
                try: os.remove(os.path.join(DINGUZPLUS_THUMBNAILS, safe+"_vertical"+ext))
                except: pass
        # trailer
        if 'trailer' in request.files:
            tr=request.files['trailer']
            if tr.filename and tr.filename.lower().endswith('.mp4'):
                # limit 100MB
                tr.save(os.path.join(DINGUZPLUS_TRAILERS, safe+".mp4"))
        if request.form.get('remove_trailer')=='1':
            for cand in [os.path.join(DINGUZPLUS_TRAILERS, safe+".mp4"), os.path.join(DINGUZPLUS_TRAILERS, safe+"_trailer.mp4")]:
                try:
                    if os.path.exists(cand): os.remove(cand)
                except: pass
        return jsonify({"ok":True, "meta": meta})

@app.route("/api/dinguzplus/admin/delete/<movie_id>", methods=["POST","DELETE"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_delete(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe:
        return jsonify({"error":"invalid"}),400
    mpath=_dinguzplus_movie_path(safe)
    if mpath and os.path.exists(mpath):
        try: os.remove(mpath)
        except: pass
    # remove metadata
    mp=_dinguzplus_metadata_path(safe)
    if mp and os.path.exists(mp):
        try: os.remove(mp)
        except: pass
    # remove thumbnail (any ext)
    for ext in _DINGUZPLUS_THUMB_EXTS:
        for suf in ["", "_vertical"]:
            tp=os.path.join(DINGUZPLUS_THUMBNAILS, safe+suf+ext)
            if os.path.exists(tp):
                try: os.remove(tp)
                except: pass
    # also from resources poster fallback
    for ext in ['.jpg','.jpeg','.png','.webp']:
        pp=os.path.join(DINGUZPLUS_RESOURCES, safe+ext)
        if os.path.exists(pp):
            try: os.remove(pp)
            except: pass
    # remove previews
    preview_dir=_dinguzplus_preview_dir(safe)
    if preview_dir and os.path.isdir(preview_dir):
        try: shutil.rmtree(preview_dir)
        except: pass
    # remove from hero if needed
    cfg=_dinguzplus_load_config()
    if cfg.get('hero_id')==safe:
        cfg['hero_id']=None
        _dinguzplus_save_config(cfg)
    # also from hero_carousel
    if safe in cfg.get('hero_carousel',[]):
        cfg['hero_carousel']=[x for x in cfg['hero_carousel'] if x!=safe]
        _dinguzplus_save_config(cfg)
    # clean progress
    prog=_dinguzplus_load_progress()
    changed=False
    for did in list(prog.keys()):
        for pid in list(prog[did].keys()):
            if safe in prog[did][pid]:
                del prog[did][pid][safe]
                changed=True
    if changed:
        _dinguzplus_save_progress(prog)
    return jsonify({"ok":True})

@app.route("/api/dinguzplus/admin/order", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_order():
    j=request.json or {}
    order=j.get('order') or []
    if not isinstance(order, list):
        return jsonify({"error":"invalid"}),400
    for idx, mid in enumerate(order):
        safe=_dinguzplus_safe_id(str(mid))
        if not safe: continue
        mp=_dinguzplus_metadata_path(safe)
        if not mp or not os.path.exists(mp):
            continue
        try:
            with open(mp,'r') as f: data=json.load(f)
            data['order']=idx
            with open(mp,'w') as f: json.dump(data,f,indent=2)
        except: pass
    return jsonify({"ok":True})

@app.route("/api/dinguzplus/admin/hero", methods=["GET","POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_hero():
    if request.method=="GET":
        cfg=_dinguzplus_load_config()
        return jsonify(cfg)
    j=request.json or {}
    hero_id=j.get('hero_id')
    if hero_id:
        safe=_dinguzplus_safe_id(str(hero_id))
        if not safe or not _dinguzplus_movie_path(safe):
            return jsonify({"error":"not found"}),404
        cfg=_dinguzplus_load_config()
        cfg['hero_id']=safe
        _dinguzplus_save_config(cfg)
        return jsonify({"ok":True, "hero_id": safe})
    else:
        cfg=_dinguzplus_load_config()
        cfg['hero_id']=None
        _dinguzplus_save_config(cfg)
        return jsonify({"ok":True})

# HERO carousel up to 3
@app.route("/api/dinguzplus/admin/hero_carousel", methods=["GET","POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_hero_carousel():
    cfg=_dinguzplus_load_config()
    if request.method=="GET":
        return jsonify({"hero_carousel": cfg.get("hero_carousel", [])})
    j=request.json or {}
    arr=j.get("hero_carousel") or []
    if not isinstance(arr, list) or len(arr)>3:
        return jsonify({"error":"max 3"}),400
    clean=[]
    for mid in arr:
        safe=_dinguzplus_safe_id(str(mid))
        if safe and _dinguzplus_movie_path(safe):
            clean.append(safe)
    cfg["hero_carousel"]=clean
    # keep legacy hero_id as first
    if clean: cfg["hero_id"]=clean[0]
    _dinguzplus_save_config(cfg)
    return jsonify({"ok":True, "hero_carousel": clean})

@app.route("/api/dinguzplus/admin/stats/<movie_id>", methods=["GET"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_stats(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe or not _dinguzplus_movie_path(safe):
        return jsonify({"error":"not found"}),404
    meta=_dinguzplus_get_metadata(safe)
    views=_dinguzplus_load_views().get(safe,0)
    ratings=_dinguzplus_load_ratings().get(safe,{})
    likes=sum(1 for v in ratings.values() if v=="like")
    dislikes=sum(1 for v in ratings.values() if v=="dislike")
    # progress stats
    prog=_dinguzplus_load_progress()
    starts=0; completions=0; total_percent=0; total_time=0; count=0; users=set()
    for did in prog:
        for pid in prog[did]:
            if safe in prog[did][pid]:
                entry=prog[did][pid][safe]
                starts+=1
                if entry.get('completed'): completions+=1
                if entry.get('duration'):
                    total_percent+= (entry.get('position',0)/entry.get('duration',1)*100)
                    total_time+= entry.get('position',0)
                    count+=1
                users.add(did)
    avg_percent=round(total_percent/count,1) if count else 0
    avg_time=round(total_time/count,1) if count else 0
    # mylist
    mylist=_dinguzplus_load_mylist()
    mylist_count=sum(1 for did in mylist for pid in mylist[did] if safe in mylist[did][pid])
    return jsonify({
        "title": meta.get('movie_title'),
        "views": views,
        "starts": starts,
        "completions": completions,
        "avg_percent": avg_percent,
        "avg_time": avg_time,
        "likes": likes,
        "dislikes": dislikes,
        "mylist": mylist_count,
        "users": len(users)
    })

# --- MY LIST ---
@app.route("/api/dinguzplus/mylist", methods=["GET"])
@requires_authorization
def api_dinguzplus_mylist_get():
    user=get_discord_user()
    did=str(user.get('id'))
    pid=_dinguzplus_get_active_profile()
    if not pid: return jsonify({"error":"no profile"}),400
    data=_dinguzplus_load_mylist()
    lst=data.get(did,{}).get(pid,[])
    return jsonify(lst)

@app.route("/api/dinguzplus/mylist/toggle", methods=["POST"])
@requires_authorization
def api_dinguzplus_mylist_toggle():
    user=get_discord_user()
    did=str(user.get('id'))
    pid=_dinguzplus_get_active_profile()
    if not pid: return jsonify({"error":"no profile"}),400
    j=request.json or {}
    mid=_dinguzplus_safe_id(str(j.get('movie_id') or j.get('id') or ''))
    if not mid or not _dinguzplus_movie_path(mid):
        # also check series
        if not mid: return jsonify({"error":"invalid"}),400
    data=_dinguzplus_load_mylist()
    if did not in data: data[did]={}
    if pid not in data[did]: data[did][pid]=[]
    lst=data[did][pid]
    if mid in lst:
        lst.remove(mid)
        added=False
    else:
        lst.append(mid)
        added=True
    _dinguzplus_save_mylist(data)
    return jsonify({"ok":True, "added": added, "list": lst})

# --- RATINGS ---
@app.route("/api/dinguzplus/ratings/<movie_id>", methods=["GET"])
@requires_authorization
def api_dinguzplus_ratings_get(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe: return jsonify({"error":"invalid"}),400
    # allow series: check existence as movie OR series (series IDs are valid for ratings)
    is_valid = _dinguzplus_movie_path(safe)
    if not is_valid:
        try:
            series=_dinguzplus_load_series()
            if any(x['id']==safe for x in series):
                is_valid=True
        except: pass
        if not is_valid:
            # also allow any composite episode id (still valid for ratings if needed)
            # but for series itself, we already checked
            pass
    ratings=_dinguzplus_load_ratings()
    entry=ratings.get(safe, {})
    likes=sum(1 for v in entry.values() if v=="like")
    dislikes=sum(1 for v in entry.values() if v=="dislike")
    total=likes+dislikes
    pct_like=round(likes/total*100) if total else 0
    # user's vote
    user=get_discord_user()
    did=str(user.get('id')); pid=_dinguzplus_get_active_profile()
    my=None
    if did and pid:
        key=f"{did}:{pid}"
        my=entry.get(key)
    return jsonify({"likes": likes, "dislikes": dislikes, "total": total, "pct_like": pct_like, "my": my})

@app.route("/api/dinguzplus/ratings/<movie_id>", methods=["POST"])
@requires_authorization
def api_dinguzplus_ratings_post(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    # allow both movies and series (check movie path or series path)
    is_series=False
    if not _dinguzplus_movie_path(safe):
        # check series
        try:
            series=_dinguzplus_load_series()
            if any(x['id']==safe for x in series):
                is_series=True
        except: pass
        if not is_series:
            return jsonify({"error":"not found"}),404
    j=request.json or {}
    vote=j.get('vote')
    if vote not in ["like","dislike", None, ""]:
        return jsonify({"error":"invalid vote"}),400
    user=get_discord_user()
    did=str(user.get('id')); pid=_dinguzplus_get_active_profile()
    if not pid: return jsonify({"error":"no profile"}),400
    key=f"{did}:{pid}"
    ratings=_dinguzplus_load_ratings()
    if safe not in ratings: ratings[safe]={}
    if not vote:
        ratings[safe].pop(key, None)
    else:
        ratings[safe][key]=vote
    _dinguzplus_save_ratings(ratings)
    return jsonify({"ok":True})

# --- VIEWS ---
@app.route("/api/dinguzplus/views/<movie_id>", methods=["POST"])
@requires_authorization
def api_dinguzplus_views_inc(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe or not _dinguzplus_movie_path(safe):
        return jsonify({"error":"not found"}),404
    views=_dinguzplus_load_views()
    views[safe]=int(views.get(safe,0))+1
    _dinguzplus_save_views(views)
    return jsonify({"ok":True, "views": views[safe]})

@app.route("/api/dinguzplus/views", methods=["GET"])
@requires_authorization
def api_dinguzplus_views_all():
    views=_dinguzplus_load_views()
    return jsonify(views)

# --- HISTORY ---
@app.route("/api/dinguzplus/history", methods=["GET"])
@requires_authorization
def api_dinguzplus_history_get():
    user=get_discord_user()
    did=str(user.get('id')); pid=_dinguzplus_get_active_profile()
    if not pid: return jsonify([]),400
    hist=_dinguzplus_load_history()
    lst=hist.get(did,{}).get(pid,[])
    return jsonify(lst)

@app.route("/api/dinguzplus/history", methods=["POST"])
@requires_authorization
def api_dinguzplus_history_post():
    user=get_discord_user()
    did=str(user.get('id')); pid=_dinguzplus_get_active_profile()
    if not pid: return jsonify({"error":"no profile"}),400
    j=request.json or {}
    mid=_dinguzplus_safe_id(str(j.get('movie_id') or j.get('id') or ''))
    if not mid or not _dinguzplus_movie_path(mid):
        return jsonify({"error":"invalid"}),400
    hist=_dinguzplus_load_history()
    if did not in hist: hist[did]={}
    if pid not in hist[did]: hist[did][pid]=[]
    lst=hist[did][pid]
    # remove existing
    lst=[x for x in lst if x.get('id')!=mid]
    lst.insert(0, {"id": mid, "watched_at": datetime.datetime.now().isoformat()})
    # keep 50
    lst=lst[:50]
    hist[did][pid]=lst
    _dinguzplus_save_history(hist)
    return jsonify({"ok":True})

# --- CATEGORIES ---
@app.route("/api/dinguzplus/categories", methods=["GET"])
@requires_authorization
def api_dinguzplus_categories_get():
    cfg=_dinguzplus_load_categories()
    return jsonify(cfg)

@app.route("/api/dinguzplus/admin/categories", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_categories_post():
    j=request.json or {}
    cats=j.get('categories')
    if not isinstance(cats, list):
        return jsonify({"error":"invalid"}),400
    clean=[str(c).strip() for c in cats if str(c).strip()]
    if not clean: return jsonify({"error":"empty"}),400
    _dinguzplus_save_categories({"categories": clean})
    return jsonify({"ok":True, "categories": clean})

# --- SERIES ---
@app.route("/api/dinguzplus/series", methods=["GET"])
@requires_authorization
def api_dinguzplus_series_list():
    series=_dinguzplus_load_series()
    return jsonify(series)

@app.route("/api/dinguzplus/admin/series", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_series_create():
    j=request.json or {}
    title=(j.get('title') or '').strip()
    if not title: return jsonify({"error":"tytuł wymagany"}),400
    safe=_dinguzplus_safe_id(title.lower().replace(' ','_'))
    if not safe: safe=''.join(random.choices(string.ascii_lowercase+string.digits,k=8))
    # ensure unique
    sp=_dinguzplus_get_series_path(safe)
    if os.path.exists(sp):
        safe=safe+"_"+''.join(random.choices(string.digits,k=3))
        sp=_dinguzplus_get_series_path(safe)
    os.makedirs(sp, exist_ok=True)
    age_val=int(j.get('age') or 16)
    raw_tags=j.get('content_tags') or j.get('tags') or []
    if isinstance(raw_tags, str):
        try:
            import json as js2
            if raw_tags.strip().startswith('['):
                raw_tags=js2.loads(raw_tags)
            else:
                raw_tags=[t.strip() for t in raw_tags.split(',') if t.strip()]
        except:
            raw_tags=[]
    if not isinstance(raw_tags, list):
        raw_tags=[]
    allowed=_DINGUZPLUS_CONTENT_TAGS.get(age_val, [])
    if age_val in [3,7]:
        raw_tags=[]
    else:
        raw_tags=[t for t in raw_tags if t in allowed]
    meta={"title": title, "description": j.get('description',''), "age": age_val, "categories": j.get('categories') or ["Seriale"], "order": int(j.get('order') or 999), "thumbnail": None, "poster_vertical": None, "content_tags": raw_tags}
    with open(os.path.join(sp,"meta.json"),"w") as f: json.dump(meta,f,indent=2)
    # auto-create season_1
    s1=os.path.join(sp,"season_1")
    os.makedirs(s1, exist_ok=True)
    return jsonify({"ok":True, "id": safe, "meta": meta})

@app.route("/api/dinguzplus/admin/series/<sid>", methods=["DELETE"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_series_delete(sid):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if sp and os.path.isdir(sp):
        shutil.rmtree(sp)
        return jsonify({"ok":True})
    return jsonify({"error":"not found"}),404

@app.route("/api/dinguzplus/admin/series/<sid>/meta", methods=["POST","PUT"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_series_meta(sid):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"not found"}),404
    # accept both FormData and JSON
    if request.is_json:
        j=request.json or {}
    elif request.form:
        j=request.form.to_dict() if request.form else {}
    else:
        j={}
    meta_path=os.path.join(sp, "meta.json")
    meta={}
    if os.path.exists(meta_path):
        try:
            with open(meta_path,'r') as f: meta=json.load(f)
        except: meta={}
    if 'title' in j: meta['title']=str(j.get('title','')).strip() or meta.get('title',safe)
    if 'description' in j: meta['description']=str(j.get('description',''))
    if 'age' in j or 'age_restriction' in j:
        try: meta['age']=int(j.get('age',j.get('age_restriction'))); meta['age_restriction']=meta['age']
        except: pass
    if 'categories' in j:
        cats=j.get('categories') or []
        if isinstance(cats, str): cats=[c.strip() for c in cats.split(',') if c.strip()]
        if not isinstance(cats, list): cats=[]
        meta['categories']=cats
    if 'order' in j:
        try: meta['order']=int(j.get('order'))
        except: pass
    if 'premiere_at' in j:
        meta['premiere_at']=j.get('premiere_at') or None
    if 'content_tags' in j:
        tags=j.get('content_tags') or []
        if isinstance(tags, str):
            try:
                if tags.strip().startswith('['):
                    tags=json.loads(tags)
                else:
                    tags=[t.strip() for t in tags.split(',') if t.strip()]
            except: tags=[]
        av=int(meta.get('age',meta.get('age_restriction',16)))
        if av in [3,7]:
            tags=[]
        else:
            allowed=_DINGUZPLUS_CONTENT_TAGS.get(av, [])
            tags=[t for t in tags if t in allowed]
        meta['content_tags']=tags
    with open(meta_path,'w') as f: json.dump(meta,f,indent=2)
    return jsonify({"ok":True, "meta": meta})

@app.route("/api/dinguzplus/admin/series/<sid>/asset/<asset_type>", methods=["POST","DELETE"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_series_asset(sid, asset_type):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"not found"}),404
    if request.method=="DELETE":
        # delete asset
        if asset_type in ('thumbnail','poster_vertical'):
            thumb_dir=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe)
            for f in os.listdir(thumb_dir) if os.path.isdir(thumb_dir) else []:
                os.remove(os.path.join(thumb_dir,f))
        elif asset_type=='trailer':
            # remove trailer from all seasons
            for sea_name in os.listdir(sp):
                sea_dir=os.path.join(sp, sea_name)
                if os.path.isdir(sea_dir):
                    for f in os.listdir(sea_dir):
                        if f.endswith('_trailer.mp4'):
                            os.remove(os.path.join(sea_dir, f))
        return jsonify({"ok":True})
    # POST: upload asset
    if 'file' not in request.files:
        return jsonify({"error":"Brak pliku"}),400
    f=request.files['file']
    if asset_type=='thumbnail':
        ext='.png'
        if not f.filename.lower().endswith(('.png','.jpg','.jpeg','.webp')):
            return jsonify({"error":"format"}),400
        target_dir=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe)
        os.makedirs(target_dir, exist_ok=True)
        out_path=os.path.join(target_dir, f"thumb{os.path.splitext(f.filename)[1].lower()}")
        f.save(out_path)
    elif asset_type=='poster_vertical':
        ext='.png'
        if not f.filename.lower().endswith(('.png','.jpg','.jpeg','.webp')):
            return jsonify({"error":"format"}),400
        target_dir=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe)
        os.makedirs(target_dir, exist_ok=True)
        out_path=os.path.join(target_dir, f"thumb_vertical{os.path.splitext(f.filename)[1].lower()}")
        f.save(out_path)
    elif asset_type=='trailer':
        if not f.filename.lower().endswith('.mp4'):
            return jsonify({"error":"format"}),400
        target_dir=os.path.join(DINGUZPLUS_TRAILERS, "series", safe)
        os.makedirs(target_dir, exist_ok=True)
        out_path=os.path.join(target_dir, "trailer.mp4")
        f.save(out_path)
    else:
        return jsonify({"error":"unknown asset"}),400
    return jsonify({"ok":True, "path": out_path})

@app.route("/api/dinguzplus/admin/series/<sid>/season", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_series_season_create(sid):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"not found"}),404
    j=request.json or {}
    name=(j.get('name') or '').strip()
    if not name:
        # auto-generate next season number
        existing=[d for d in os.listdir(sp) if d.startswith("season_") and d[8:].isdigit()]
        n=1
        if existing:
            nums=sorted([int(d[8:]) for d in existing])
            n=nums[-1]+1
        name=f"season_{n}"
    safe_name=secure_filename(name)
    season_dir=os.path.join(sp, safe_name)
    if not safe_name.startswith("season_"):
        safe_name="season_"+safe_name
    season_dir=os.path.join(sp, safe_name)
    os.makedirs(season_dir, exist_ok=True)
    return jsonify({"ok":True, "name": safe_name})

@app.route("/api/dinguzplus/admin/series/<sid>/<season_id>/reorder", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_series_season_reorder(sid, season_id):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"not found"}),404
    safe_season=secure_filename(season_id)
    season_dir=os.path.join(sp, safe_season)
    if not os.path.isdir(season_dir):
        return jsonify({"error":"season not found"}),404
    # rename to next available season_N
    existing=[d for d in os.listdir(sp) if d.startswith("season_") and d[8:].isdigit() and d!=safe_season]
    if existing:
        nums=sorted([int(d[8:]) for d in existing])
        max_n=nums[-1]
    else:
        max_n=0
    new_n=max_n+1
    new_name=f"season_{new_n}"
    new_dir=os.path.join(sp, new_name)
    shutil.move(season_dir, new_dir)
    return jsonify({"ok":True, "name": new_name})

@app.route("/api/dinguzplus/admin/series/<sid>/<season_id>", methods=["DELETE"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_series_season_delete(sid, season_id):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"not found"}),404
    safe_season=secure_filename(season_id)
    season_dir=os.path.join(sp, safe_season)
    if not os.path.isdir(season_dir):
        return jsonify({"error":"season not found"}),404
    # remove all episodes and trailer
    shutil.rmtree(season_dir)
    return jsonify({"ok":True})

@app.route("/api/dinguzplus/admin/series/<sid>/<season_id>/<ep_id>/meta", methods=["POST","PUT"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_episode_meta(sid, season_id, ep_id):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"not found"}),404
    safe_season=secure_filename(season_id)
    safe_ep=secure_filename(ep_id)
    season_dir=os.path.join(sp, safe_season)
    if not os.path.isdir(season_dir):
        return jsonify({"error":"season not found"}),404
    ep_meta_path=os.path.join(season_dir, f"{safe_ep}.json")
    ep_meta={}
    if os.path.exists(ep_meta_path):
        try:
            with open(ep_meta_path,'r') as f: ep_meta=json.load(f)
        except: ep_meta={}
    if request.is_json:
        j=request.json or {}
    elif request.form:
        j=request.form.to_dict() if request.form else {}
    else:
        j={}
    if 'title' in j: ep_meta['title']=str(j.get('title','')).strip() or ep_meta.get('title',safe_ep)
    if 'description' in j: ep_meta['description']=str(j.get('description',''))
    if 'age' in j or 'age_restriction' in j:
        try: ep_meta['age']=int(j.get('age',j.get('age_restriction'))); ep_meta['age_restriction']=ep_meta['age']
        except: pass
    if 'premiere_at' in j: ep_meta['premiere_at']=j.get('premiere_at') or None
    if 'order' in j:
        try: ep_meta['order']=int(j.get('order'))
        except: pass
    if 'content_tags' in j:
        tags=j.get('content_tags') or []
        if isinstance(tags, str):
            try:
                if tags.strip().startswith('['):
                    tags=json.loads(tags)
                else:
                    tags=[t.strip() for t in tags.split(',') if t.strip()]
            except: tags=[]
        av=int(ep_meta.get('age', ep_meta.get('age_restriction',16)))
        if av in [3,7]:
            tags=[]
        else:
            allowed=_DINGUZPLUS_CONTENT_TAGS.get(av, [])
            tags=[t for t in tags if t in allowed]
        ep_meta['content_tags']=tags
    with open(ep_meta_path,'w') as f: json.dump(ep_meta,f,indent=2)
    return jsonify({"ok":True, "meta": ep_meta})

@app.route("/api/dinguzplus/admin/series/<sid>/<season_id>/<ep_id>/asset/<asset_type>", methods=["POST","DELETE"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_episode_asset(sid, season_id, ep_id, asset_type):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"not found"}),404
    safe_season=secure_filename(season_id)
    safe_ep=secure_filename(ep_id)
    season_dir=os.path.join(sp, safe_season)
    if not os.path.isdir(season_dir):
        return jsonify({"error":"season not found"}),404
    if request.method=="DELETE":
        # delete asset
        if asset_type in ('thumbnail','poster_vertical'):
            thumb_dir=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe, safe_season)
            if os.path.isdir(thumb_dir):
                for f in os.listdir(thumb_dir):
                    if f.startswith(safe_ep):
                        os.remove(os.path.join(thumb_dir, f))
        elif asset_type=='trailer':
            for f in os.listdir(season_dir):
                if f.startswith(safe_ep) and f.endswith('_trailer.mp4'):
                    os.remove(os.path.join(season_dir, f))
        return jsonify({"ok":True})
    if 'file' not in request.files:
        return jsonify({"error":"Brak pliku"}),400
    f=request.files['file']
    if asset_type=='thumbnail':
        if not f.filename.lower().endswith(('.png','.jpg','.jpeg','.webp')):
            return jsonify({"error":"format"}),400
        thumb_dir=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe, safe_season)
        os.makedirs(thumb_dir, exist_ok=True)
        out_path=os.path.join(thumb_dir, f"{safe_ep}{os.path.splitext(f.filename)[1].lower()}")
        f.save(out_path)
    elif asset_type=='poster_vertical':
        if not f.filename.lower().endswith(('.png','.jpg','.jpeg','.webp')):
            return jsonify({"error":"format"}),400
        thumb_dir=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe, safe_season)
        os.makedirs(thumb_dir, exist_ok=True)
        out_path=os.path.join(thumb_dir, f"{safe_ep}_vertical{os.path.splitext(f.filename)[1].lower()}")
        f.save(out_path)
    elif asset_type=='trailer':
        if not f.filename.lower().endswith('.mp4'):
            return jsonify({"error":"format"}),400
        out_path=os.path.join(season_dir, f"{safe_ep}_trailer.mp4")
        f.save(out_path)
    else:
        return jsonify({"error":"unknown asset"}),400
    return jsonify({"ok":True, "path": out_path})

@app.route("/api/dinguzplus/admin/series/<sid>/<season_id>/<ep_id>/reorder", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_episode_reorder(sid, season_id, ep_id):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"not found"}),404
    safe_season=secure_filename(season_id)
    season_dir=os.path.join(sp, safe_season)
    if not os.path.isdir(season_dir):
        return jsonify({"error":"season not found"}),404
    safe_ep=secure_filename(ep_id)
    if not os.path.exists(os.path.join(season_dir, f"{safe_ep}.mp4")) and not os.path.exists(os.path.join(season_dir, f"{safe_ep}.json")):
        return jsonify({"error":"episode not found"}),404
    j=request.json or {}
    # Support both dir swap and full order list (for drag & drop)
    if 'order' in j and isinstance(j['order'], list):
        # Full reorder list provided: j['order'] is list of episode ids in new order
        new_order=[secure_filename(x) for x in j['order'] if secure_filename(x)]
        # validate all ids exist
        existing=set(f[:-5] for f in os.listdir(season_dir) if f.endswith('.json'))
        new_order=[x for x in new_order if x in existing]
        # append any missing (should not happen)
        for eid in existing:
            if eid not in new_order:
                new_order.append(eid)
        for i, ep in enumerate(new_order):
            old=os.path.join(season_dir, f"{ep}.json")
            if os.path.exists(old):
                try:
                    with open(old) as f: m=json.load(f)
                    m['order']=i
                    with open(old,'w') as f: json.dump(m,f,indent=2)
                except: pass
        return jsonify({"ok":True, "order": new_order})
    dir=int(j.get('dir') or 0)
    if dir not in (-1, 1):
        return jsonify({"error":"dir must be -1 or 1"}),400
    # Load all episodes with their current order
    eps_data=[]
    for f in os.listdir(season_dir):
        if not f.endswith('.json'): continue
        base=os.path.splitext(f)[0]
        try:
            with open(os.path.join(season_dir, f)) as jf: m=json.load(jf)
            order=int(m.get('order', 999))
        except:
            order=999
        eps_data.append((order, base))
    eps_data.sort(key=lambda x: (x[0], x[1]))
    ordered=[base for _, base in eps_data]
    try:
        idx=ordered.index(safe_ep)
    except ValueError:
        return jsonify({"error":"episode not in list"}),404
    # swap with neighbor
    if dir==-1 and idx>0:
        ordered[idx], ordered[idx-1] = ordered[idx-1], ordered[idx]
    elif dir==1 and idx < len(ordered)-1:
        ordered[idx], ordered[idx+1] = ordered[idx+1], ordered[idx]
    else:
        return jsonify({"ok":True, "order": ordered}) # no change (already at boundary)
    for i, ep in enumerate(ordered):
        old=os.path.join(season_dir, f"{ep}.json")
        if os.path.exists(old):
            try:
                with open(old) as f: m=json.load(f)
                m['order']=i
                with open(old,'w') as f: json.dump(m,f,indent=2)
            except: pass
    return jsonify({"ok":True, "order": ordered})

@app.route("/api/dinguzplus/admin/series/<sid>/<season_id>/<ep_id>/episode", methods=["DELETE"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_episode_delete_url(sid, season_id, ep_id):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"series not found"}),404
    safe_season=secure_filename(season_id)
    season_dir=os.path.join(sp, safe_season)
    if not os.path.isdir(season_dir):
        return jsonify({"error":"season not found"}),404
    # ep_id from URL (e.g., test1) - also check JSON body fallback
    ep_safe=secure_filename(ep_id)
    # also allow body id override if provided
    try:
        j=request.json or {}
        body_id=j.get('id') or j.get('name')
        if body_id:
            # if body id differs, use it (for compatibility)
            body_safe=secure_filename(body_id)
            if body_safe != ep_safe:
                # log mismatch but use URL param as primary
                pass
    except: pass
    for f in os.listdir(season_dir):
        if os.path.splitext(f)[0]==ep_safe and f.lower().endswith('.mp4'):
            try: os.remove(os.path.join(season_dir,f))
            except: pass
    meta_p=os.path.join(season_dir, ep_safe+".json")
    if os.path.exists(meta_p):
        try: os.remove(meta_p)
        except: pass
    sub_p=os.path.join(DINGUZPLUS_SUBTITLES, "series", safe, safe_season, ep_safe)
    if os.path.isdir(sub_p):
        shutil.rmtree(sub_p, ignore_errors=True)
    # also clean up thumbnails for this episode
    thumb_dir=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe)
    if os.path.isdir(thumb_dir):
        for tf in [f"{safe_season}_{ep_safe}.png", f"{safe_season}_{ep_safe}_vertical.png"]:
            tp=os.path.join(thumb_dir, tf)
            if os.path.exists(tp):
                try: os.remove(tp)
                except: pass
    # clean up previews
    comp_id=f"{safe}_{safe_season}_{ep_safe}"
    preview_dir=_dinguzplus_preview_dir(comp_id)
    if preview_dir and os.path.isdir(preview_dir):
        shutil.rmtree(preview_dir, ignore_errors=True)
    # also try old preview path for backward compat
    old_preview=_dinguzplus_preview_dir(ep_safe)
    if old_preview and os.path.isdir(old_preview):
        # only delete if it looks like episode preview (check if parent contains season)
        pass
    return jsonify({"ok":True})

@app.route("/api/dinguzplus/admin/series/<sid>/<season_id>/episode", methods=["POST","DELETE"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_series_episode(sid, season_id):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"series not found"}),404
    safe_season=secure_filename(season_id)
    season_dir=os.path.join(sp, safe_season)
    if not os.path.isdir(season_dir):
        return jsonify({"error":"season not found"}),404
    if request.method=="DELETE":
        # delete episode by name
        j=request.json or {}
        ep_id=j.get('id') or j.get('name')
        if not ep_id: return jsonify({"error":"id required"}),400
        ep_safe=secure_filename(ep_id)
        for f in os.listdir(season_dir):
            if os.path.splitext(f)[0]==ep_safe and f.lower().endswith('.mp4'):
                try: os.remove(os.path.join(season_dir,f))
                except: pass
        meta_p=os.path.join(season_dir, ep_safe+".json")
        if os.path.exists(meta_p):
            try: os.remove(meta_p)
            except: pass
        sub_p=os.path.join(DINGUZPLUS_SUBTITLES, "series", safe, safe_season, ep_safe)
        if os.path.isdir(sub_p):
            shutil.rmtree(sub_p, ignore_errors=True)
        return jsonify({"ok":True})
    # POST: create episode record first (chunked upload will target it)
    j=request.json or {}
    ep_title=(j.get('title') or '').strip() or 'Episode'
    base=secure_filename(re.sub(r'[^a-zA-Z0-9_-]+','_', ep_title.lower().replace(' ','_')))
    if not base: base='episode'
    # ensure unique
    n=0
    while os.path.exists(os.path.join(season_dir, f"{base}{('_'+str(n)) if n else ''}.mp4")) or os.path.exists(os.path.join(season_dir, f"{base}{('_'+str(n)) if n else ''}.json")):
        n+=1
    ep_id=base if n==0 else f"{base}_{n}"
    ep_dir=season_dir
    ep_meta_path=os.path.join(ep_dir, ep_id+".json")
    if os.path.exists(ep_meta_path):
        return jsonify({"error":"id already exists"}),400
    age_val=int(j.get('age') or 16)
    raw_tags=j.get('content_tags') or j.get('tags') or []
    if isinstance(raw_tags, str):
        try:
            import json as js2
            if raw_tags.strip().startswith('['):
                raw_tags=js2.loads(raw_tags)
            else:
                raw_tags=[t.strip() for t in raw_tags.split(',') if t.strip()]
        except:
            raw_tags=[]
    if not isinstance(raw_tags, list):
        raw_tags=[]
    allowed=_DINGUZPLUS_CONTENT_TAGS.get(age_val, [])
    if age_val in [3,7]:
        raw_tags=[]
    else:
        raw_tags=[t for t in raw_tags if t in allowed]
    # determine order: if provided and not 999, use it, else compute next sequential
    order_val=None
    if 'order' in j and j.get('order') not in (None, '', 999, '999'):
        try:
            order_val=int(j.get('order'))
        except:
            order_val=None
    if order_val is None:
        # compute max existing order +1 for new episode
        max_order=-1
        for f in os.listdir(season_dir):
            if f.endswith('.json'):
                try:
                    with open(os.path.join(season_dir, f)) as jf:
                        m=json.load(jf)
                        max_order=max(max_order, int(m.get('order', -1)))
                except:
                    pass
        order_val=max_order+1
    meta={"title": ep_title, "description": j.get('description',''), "age": age_val, "premiere_at": None, "order": order_val, "thumbnail": None, "poster_vertical": None, "trailer": None, "subtitles": [], "content_tags": raw_tags}
    # normalize premiere_at
    pa=j.get('premiere_at')
    if pa:
        try:
            from zoneinfo import ZoneInfo
            dt=datetime.datetime.fromisoformat(str(pa).replace('Z',''))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
            dt=dt.astimezone(datetime.timezone.utc)
            meta['premiere_at']=dt.isoformat().replace("+00:00","Z")
        except: pass
    with open(ep_meta_path,"w") as f: json.dump(meta,f,indent=2)
    return jsonify({"ok":True, "id": ep_id, "series_id": safe, "season_id": safe_season, "meta": meta})

@app.route("/api/dinguzplus/admin/series/<sid>/<season_id>/<ep_id>/init", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_episode_init(sid, season_id, ep_id):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"series not found"}),404
    safe_season=secure_filename(season_id)
    safe_ep=secure_filename(ep_id)
    season_dir=os.path.join(sp, safe_season)
    if not os.path.isdir(season_dir):
        return jsonify({"error":"season not found"}),404
    meta_p=os.path.join(season_dir, safe_ep+".json")
    if not os.path.exists(meta_p):
        return jsonify({"error":"episode not initialized"}),404
    j=request.json or {}
    filename=secure_filename(j.get('filename') or '')
    if not filename.lower().endswith('.mp4'):
        return jsonify({"error":"tylko .mp4"}),400
    size=int(j.get('size') or 0)
    total=int(j.get('totalChunks') or 0)
    if size<=0 or total<=0:
        return jsonify({"error":"Nieprawidłowy rozmiar"}),400
    upload_id=uuid.uuid4().hex
    tmp_dir=os.path.join(season_dir, f".tmp_{upload_id}_{safe_ep}")
    os.makedirs(tmp_dir, exist_ok=True)
    meta={"uploadId": upload_id, "series_id": safe, "season_id": safe_season, "episode_id": safe_ep, "filename": filename, "size": size, "totalChunks": total, "tmp_dir": tmp_dir, "created": datetime.datetime.now().isoformat()}
    DINGUZPLUS_UPLOAD_SESSIONS[upload_id]=meta
    try:
        with open(os.path.join(tmp_dir, ".meta.json"),"w") as f: json.dump(meta,f)
    except: pass
    return jsonify({"uploadId": upload_id})

@app.route("/api/dinguzplus/admin/series/<sid>/<season_id>/<upload_id>/chunk", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_episode_chunk(sid, season_id, upload_id):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"series not found"}),404
    upload_id_str=upload_id
    # safe_season is the season id, not used here but available
    meta=DINGUZPLUS_UPLOAD_SESSIONS.get(upload_id_str)
    if not meta:
        return jsonify({"error":"Nieznany uploadId"}),404
    tmp_dir=meta['tmp_dir']
    if not os.path.isdir(tmp_dir):
        return jsonify({"error":"Sesja wygasła"}),404
    chunk=request.files.get('file')
    if not chunk:
        return jsonify({"error":"Brak chunku"}),400
    chunk_path=os.path.join(tmp_dir, f"chunk_{int(request.form.get('chunkIndex') or 0):06d}")
    with open(chunk_path+".tmp","wb") as out:
        for b in iter(lambda: chunk.read(8192), b""):
            out.write(b)
    os.replace(chunk_path+".tmp", chunk_path)
    return jsonify({"ok":True, "chunk": int(request.form.get('chunkIndex') or 0)})

@app.route("/api/dinguzplus/admin/series/<sid>/<season_id>/<ep_id>/complete", methods=["POST"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_episode_complete(sid, season_id, ep_id):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"series not found"}),404
    safe_season=secure_filename(season_id)
    safe_ep=secure_filename(ep_id)
    season_dir=os.path.join(sp, safe_season)
    if not os.path.isdir(season_dir):
        return jsonify({"error":"season not found"}),404
    # accept JSON, FormData, and URL params for uploadId
    if request.is_json:
        j=request.json or {}
        upload_id=j.get('uploadId') or j.get('upload_id')
    elif request.form:
        j=request.form.to_dict() if request.form else {}
        upload_id=j.get('uploadId') or j.get('upload_id')
    else:
        j={}
        upload_id=request.args.get('uploadId') or request.args.get('upload_id')
    if not upload_id:
        return jsonify({"error":"uploadId required"}),400
    meta=DINGUZPLUS_UPLOAD_SESSIONS.get(upload_id)
    if not meta:
        # try load from disk
        try:
            for root, dirs, files in os.walk(sp):
                for d in dirs:
                    if d.startswith(f".tmp_{upload_id}_"):
                        for sson in os.listdir(os.path.join(root, d)):
                            if sson.startswith('season_'):
                                meta_p=os.path.join(root, d, sson, '.meta.json')
                                if os.path.exists(meta_p):
                                    with open(meta_p,'r') as f: meta=json.load(f)
                                    DINGUZPLUS_UPLOAD_SESSIONS[upload_id]=meta
                                    break
                if meta: break
        except: pass
    if not meta:
        return jsonify({"error":"Nieznany uploadId"}),404
    tmp_dir=meta['tmp_dir']
    chunk_files=sorted([f for f in os.listdir(tmp_dir) if f.startswith("chunk_")])
    if not chunk_files:
        return jsonify({"error":"Brak chunków"}),400
    final_path=os.path.join(season_dir, f"{safe_ep}.mp4")
    tmp_final=final_path+".assemble.tmp"
    try:
        with open(tmp_final,"wb") as out:
            for fname in chunk_files:
                p=os.path.join(tmp_dir, fname)
                with open(p,"rb") as cf:
                    shutil.copyfileobj(cf, out, length=8192)
        got=os.path.getsize(tmp_final)
        if got != int(meta.get('size', got)):
            try: os.remove(tmp_final)
            except: pass
            return jsonify({"error":f"Rozmiar mismatch {got} vs {meta.get('size')}"}),400
        if got < 1024:
            try: os.remove(tmp_final)
            except: pass
            return jsonify({"error":"Plik uszkodzony"}),400
        if os.path.exists(final_path):
            try: os.remove(final_path)
            except: pass
        os.replace(tmp_final, final_path)
        try: shutil.rmtree(tmp_dir)
        except: pass
        DINGUZPLUS_UPLOAD_SESSIONS.pop(upload_id, None)
        # Update episode meta with any extra fields from request
        ep_meta_path=os.path.join(season_dir, f"{safe_ep}.json")
        ep_meta={}
        if os.path.exists(ep_meta_path):
            try:
                with open(ep_meta_path,'r') as f: ep_meta=json.load(f)
            except: ep_meta={}
        # update fields from request
        age_val=j.get('age') or j.get('age_restriction')
        if age_val:
            try: ep_meta['age']=int(age_val); ep_meta['age_restriction']=int(age_val)
            except: pass
        if 'title' in j: ep_meta['title']=str(j.get('title','')).strip() or ep_meta.get('title','')
        if 'description' in j: ep_meta['description']=str(j.get('description',''))
        if 'order' in j:
            try: ep_meta['order']=int(j.get('order'))
            except: pass
        if 'content_tags' in j:
            raw_tags=j.get('content_tags') or []
            if isinstance(raw_tags, str):
                try:
                    import json as _jt
                    if raw_tags.strip().startswith('['):
                        raw_tags=_jt.loads(raw_tags)
                    else:
                        raw_tags=[t.strip() for t in raw_tags.split(',') if t.strip()]
                except: raw_tags=[]
            if not isinstance(raw_tags, list): raw_tags=[]
            av=int(ep_meta.get('age', ep_meta.get('age_restriction',16)))
            allowed=_DINGUZPLUS_CONTENT_TAGS.get(av, [])
            if av in [3,7]:
                raw_tags=[]
            else:
                raw_tags=[t for t in raw_tags if t in allowed]
            ep_meta['content_tags']=raw_tags
        # save duration deterministically from file
        try:
            d=_dinguzplus_duration(final_path)
            if d and d>0:
                ep_meta['duration']=round(float(d),1)
        except: pass
        with open(ep_meta_path,'w') as f: json.dump(ep_meta,f,indent=2)
        # auto-generate deterministic thumbnail if no custom exists
        try:
            thumb_p=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe, f"{safe_season}_{safe_ep}.png")
            if not os.path.exists(thumb_p):
                _dinguzplus_ensure_episode_thumbnail(safe, safe_season, safe_ep)
        except: pass
        # generate timeline previews in background (use composite id for uniqueness)
        try:
            comp_id=f"{safe}_{safe_season}_{safe_ep}"
            _dinguzplus_queue_preview(comp_id)
            # also queue old style for backward compat
            _dinguzplus_queue_preview(safe_ep)
        except: pass
        return jsonify({"ok":True, "id": safe_ep, "path": final_path, "meta": ep_meta})
    except Exception as e:
        try:
            if os.path.exists(tmp_final): os.remove(tmp_final)
        except: pass
        return jsonify({"error":str(e)}),500

@app.route("/api/dinguzplus/admin/series/<sid>/<season_id>/<ep_id>/thumb", methods=["POST","DELETE"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_episode_thumb(sid, season_id, ep_id):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"not found"}),404
    safe_season=secure_filename(season_id)
    safe_ep=secure_filename(ep_id)
    season_dir=os.path.join(sp, safe_season)
    if not os.path.isdir(season_dir):
        return jsonify({"error":"season not found"}),404
    meta_p=os.path.join(season_dir, safe_ep+".json")
    if not os.path.exists(meta_p):
        return jsonify({"error":"episode not found"}),404
    if request.method=="DELETE":
        meta=json.load(open(meta_p))
        meta.pop('thumbnail',None); meta.pop('poster_vertical',None)
        with open(meta_p,"w") as f: json.dump(meta,f,indent=2)
        return jsonify({"ok":True})
    j=request.form
    thumb_v=j.get('thumbnail_vertical')
    thumb_h=j.get('thumbnail')
    if thumb_h and thumb_h.lower().endswith('.png'):
        # use series_id/safe_ep as base
        thumb_dir=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe)
        os.makedirs(thumb_dir, exist_ok=True)
        f=request.files['thumbnail']
        f.save(os.path.join(thumb_dir, f"{safe_season}_{safe_ep}.png"))
    if thumb_v and thumb_v.lower().endswith('.png'):
        thumb_dir=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe)
        os.makedirs(thumb_dir, exist_ok=True)
        f=request.files['thumbnail_vertical']
        f.save(os.path.join(thumb_dir, f"{safe_season}_{safe_ep}_vertical.png"))
    meta=json.load(open(meta_p))
    meta['thumbnail']=f"/dinguzplus/series_thumb/{safe}/{safe_season}/{safe_ep}.png" if os.path.exists(os.path.join(DINGUZPLUS_THUMBNAILS,"series",safe,f"{safe_season}_{safe_ep}.png")) else None
    meta['poster_vertical']=f"/dinguzplus/series_thumb/{safe}/{safe_season}/{safe_ep}_vertical.png" if os.path.exists(os.path.join(DINGUZPLUS_THUMBNAILS,"series",safe,f"{safe_season}_{safe_ep}_vertical.png")) else None
    with open(meta_p,"w") as f: json.dump(meta,f,indent=2)
    return jsonify({"ok":True, "meta": meta})

@app.route("/api/dinguzplus/series_detail/<sid>")
@requires_authorization
def api_dinguzplus_series_detail(sid):
    safe=_dinguzplus_safe_id(sid)
    sp=_dinguzplus_get_series_path(safe)
    if not sp or not os.path.isdir(sp):
        return jsonify({"error":"not found"}),404
    series=_dinguzplus_load_series()
    s=[x for x in series if x['id']==safe]
    return jsonify(s[0] if s else {"id":safe, "seasons":[]})

@app.route("/dinguzplus/series_thumb/<sid>/<season_id>/<ep_id>.png")
@requires_authorization
def dinguzplus_series_thumb(sid, season_id, ep_id):
    safe=_dinguzplus_safe_id(sid)
    safe_season=secure_filename(season_id)
    safe_ep=secure_filename(ep_id.replace('.png',''))
    p=os.path.join(DINGUZPLUS_THUMBNAILS,"series",safe,f"{safe_season}_{safe_ep}.png")
    if not os.path.exists(p):
        # auto-generate deterministic thumbnail if video exists and no custom thumb
        gen=_dinguzplus_ensure_episode_thumbnail(safe, safe_season, safe_ep)
        if gen and os.path.exists(gen):
            p=gen
        else:
            return "Not found",404
    return send_file(p, mimetype="image/png")

@app.route("/dinguzplus/series_thumb/<sid>/<season_id>/<ep_id>_vertical.png")
@requires_authorization
def dinguzplus_series_thumb_vertical(sid, season_id, ep_id):
    safe=_dinguzplus_safe_id(sid)
    safe_season=secure_filename(season_id)
    safe_ep=secure_filename(ep_id.replace('_vertical.png',''))
    p=os.path.join(DINGUZPLUS_THUMBNAILS,"series",safe,f"{safe_season}_{safe_ep}_vertical.png")
    if not os.path.exists(p):
        return "Not found",404
    return send_file(p, mimetype="image/png")

# --- TRAILER ---
@app.route("/dinguzplus/trailer/<movie_id>")
@requires_authorization
def dinguzplus_trailer(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe: return "Not found",404
    # try film meta first, else series meta
    is_series=False
    meta=_dinguzplus_get_metadata(safe)
    # if no film file but series exists, treat as series
    if not _dinguzplus_movie_path(safe):
        try:
            series=_dinguzplus_load_series()
            if any(x.get('id')==safe for x in series):
                is_series=True
                # find series meta
                for s in series:
                    if s.get('id')==safe:
                        meta=s.get('meta') or {}
                        break
        except: pass
    if not _dinguzplus_is_premiered(meta) and not _is_dinguzplus_admin():
        return "Premiera wkrótce",403
    # check active profile
    if not _dinguzplus_get_active_profile() and not _is_dinguzplus_admin():
        return "Wybierz profil",403
    cands=[os.path.join(DINGUZPLUS_TRAILERS, safe+".mp4"), os.path.join(DINGUZPLUS_TRAILERS, safe+"_trailer.mp4")]
    if is_series:
        cands.append(os.path.join(DINGUZPLUS_TRAILERS, "series", safe, "trailer.mp4"))
    else:
        # also check series path fallback (for series hover if id is film-like)
        cands.append(os.path.join(DINGUZPLUS_TRAILERS, "series", safe, "trailer.mp4"))
    for cand in cands:
        if os.path.exists(cand):
            return send_file(cand, mimetype="video/mp4", conditional=True)
    return "No trailer",404

@app.route("/api/dinguzplus/admin/trailer/<movie_id>", methods=["POST","DELETE"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_trailer(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe or not _dinguzplus_movie_path(safe):
        return jsonify({"error":"not found"}),404
    if request.method=="DELETE":
        for cand in [os.path.join(DINGUZPLUS_TRAILERS, safe+".mp4"), os.path.join(DINGUZPLUS_TRAILERS, safe+"_trailer.mp4")]:
            if os.path.exists(cand):
                os.remove(cand)
        return jsonify({"ok":True})
    if 'file' not in request.files:
        return jsonify({"error":"Brak pliku"}),400
    f=request.files['file']
    if not f.filename.lower().endswith('.mp4'):
        return jsonify({"error":"tylko .mp4"}),400
    dest=os.path.join(DINGUZPLUS_TRAILERS, safe+".mp4")
    f.save(dest)
    return jsonify({"ok":True})

# --- SUBTITLES ---
@app.route("/dinguzplus/subtitle/<movie_id>/<lang>")
@requires_authorization
def dinguzplus_subtitle(movie_id, lang):
    safe=_dinguzplus_safe_id(movie_id)
    lang_safe=secure_filename(lang)
    if not safe or not lang_safe: return "Not found",404
    p=os.path.join(DINGUZPLUS_SUBTITLES, safe, lang_safe+".vtt")
    if not os.path.exists(p):
        return "Not found",404
    return send_file(p, mimetype="text/vtt")

@app.route("/api/dinguzplus/subtitles/<movie_id>", methods=["GET"])
@requires_authorization
def api_dinguzplus_subtitles_list(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe: return jsonify([]),400
    d=os.path.join(DINGUZPLUS_SUBTITLES, safe)
    if not os.path.isdir(d): return jsonify([])
    langs=[os.path.splitext(f)[0] for f in os.listdir(d) if f.lower().endswith('.vtt')]
    return jsonify(langs)

@app.route("/api/dinguzplus/admin/subtitle/<movie_id>", methods=["POST","DELETE"])
@_dinguzplus_require_admin
def api_dinguzplus_admin_subtitle(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe or not _dinguzplus_movie_path(safe):
        return jsonify({"error":"not found"}),404
    if request.method=="DELETE":
        lang=request.args.get('lang') or (request.json or {}).get('lang')
        if not lang: return jsonify({"error":"lang required"}),400
        lang_safe=secure_filename(lang)
        p=os.path.join(DINGUZPLUS_SUBTITLES, safe, lang_safe+".vtt")
        if os.path.exists(p):
            os.remove(p)
        # update metadata subtitles list
        meta=_dinguzplus_get_metadata(safe)
        if lang_safe in meta.get('subtitles',[]):
            meta['subtitles'].remove(lang_safe)
            with open(_dinguzplus_metadata_path(safe),'w') as f: json.dump(meta,f,indent=2)
        return jsonify({"ok":True})
    # POST
    if 'file' not in request.files:
        return jsonify({"error":"Brak pliku .vtt"}),400
    lang=(request.form.get('lang') or '').strip()
    if not lang: return jsonify({"error":"lang required"}),400
    lang_safe=secure_filename(lang)
    f=request.files['file']
    if not f.filename.lower().endswith('.vtt'):
        return jsonify({"error":"tylko .vtt"}),400
    dest_dir=os.path.join(DINGUZPLUS_SUBTITLES, safe)
    os.makedirs(dest_dir, exist_ok=True)
    dest=os.path.join(dest_dir, lang_safe+".vtt")
    f.save(dest)
    meta=_dinguzplus_get_metadata(safe)
    if lang_safe not in meta.get('subtitles',[]):
        meta.setdefault('subtitles',[]).append(lang_safe)
        with open(_dinguzplus_metadata_path(safe),'w') as f: json.dump(meta,f,indent=2)
    return jsonify({"ok":True, "lang": lang_safe})

# --- DETAILS PAGE ---
@app.route("/dinguzplus/details/<movie_id>")
@requires_authorization
def dinguzplus_details(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe:
        return "Not found",404
    is_series=False
    series_data=None
    mpath=_dinguzplus_movie_path(safe)
    meta=None
    if mpath and os.path.exists(mpath):
        meta=_dinguzplus_get_metadata(safe)
    else:
        # try as series
        try:
            series=_dinguzplus_load_series()
        except: series=[]
        s_match=[x for x in series if x.get('id')==safe]
        if s_match:
            is_series=True
            series_data=s_match[0]
            sm=s_match[0].get('meta') or {}
            meta=dict(sm)
            meta['is_series']=True
            meta['type']='series'
            # compute is_premiered for series (based on premiere_at in series meta, or per-episode premiere)
            meta['is_premiered']=_dinguzplus_is_premiered(sm)
            thumb_dir=os.path.join(DINGUZPLUS_THUMBNAILS, "series", safe)
            if os.path.isdir(thumb_dir):
                best_h=None; best_h_mtime=0; best_v=None; best_v_mtime=0
                for fn in os.listdir(thumb_dir):
                    low=fn.lower()
                    fp=os.path.join(thumb_dir, fn)
                    try: mt=int(os.path.getmtime(fp))
                    except: mt=0
                    if low.startswith("thumb_vertical"):
                        if best_v is None:
                            best_v=fn; best_v_mtime=mt
                    elif low.startswith("thumb."):
                        if best_h is None:
                            best_h=fn; best_h_mtime=mt
                if best_h:
                    meta['poster']=f"/dinguzplus/series_thumb_file/{safe}/{best_h}?v={best_h_mtime}"
                    meta['poster_mtime']=best_h_mtime
                if best_v:
                    meta['poster_vertical']=f"/dinguzplus/series_thumb_file/{safe}/{best_v}?v={best_v_mtime}"
                    meta['poster_vertical_mtime']=best_v_mtime
                elif best_h:
                    meta['poster_vertical']=meta.get('poster')
                    meta['poster_vertical_mtime']=best_h_mtime
        else:
            return "Not found",404
    if not is_series:
        # ensure is_premiered computed for details
        meta['is_premiered']=_dinguzplus_is_premiered(meta)
        # ensure poster fields for details (with mtime for cache bust -> hero updates together with thumb)
        thumb_p=_dinguzplus_find_thumb(safe, vertical=False)
        vert_p=_dinguzplus_find_thumb(safe, vertical=True)
        if thumb_p:
            mt=_dinguzplus_thumb_mtime(safe, False)
            meta['poster']=f"/dinguzplus/thumbnail/{safe}?v={mt}"
            meta['poster_mtime']=mt
        else:
            for ext in ['.jpg','.jpeg','.png','.webp']:
                pp=os.path.join(DINGUZPLUS_RESOURCES, safe+ext)
                if os.path.exists(pp):
                    try: mt=int(os.path.getmtime(pp))
                    except: mt=0
                    meta['poster']=f"/dinguzplus/poster/{safe}?v={mt}"
                    meta['poster_mtime']=mt
                    break
        if vert_p:
            mtv=_dinguzplus_thumb_mtime(safe, True)
            meta['poster_vertical']=f"/dinguzplus/thumbnail_vertical/{safe}?v={mtv}"
            meta['poster_vertical_mtime']=mtv
        elif thumb_p:
            meta['poster_vertical']=meta.get('poster')
            meta['poster_vertical_mtime']=meta.get('poster_mtime',0)
    # allow viewing details even before premiere (show countdown), but streaming blocked
    title=meta.get('movie_title') or meta.get('title') or _dinguzplus_pretty_name(safe)
    return render_template("dinguzplus_details.html", discord_user=get_discord_user(), movie_id=safe, meta=meta, pretty=title, is_series=is_series, series_data=series_data)

# --- NOTIFICATIONS (premiere reminder) ---
@app.route("/api/dinguzplus/notify/<movie_id>", methods=["POST","DELETE","GET"])
@requires_authorization
def api_dinguzplus_notify(movie_id):
    safe=_dinguzplus_safe_id(movie_id)
    if not safe: return jsonify({"error":"invalid"}),400
    user=get_discord_user(); did=str(user.get('id')); pid=_dinguzplus_get_active_profile()
    if not pid: return jsonify({"error":"no profile"}),400
    data=_load_json_file(DINGUZPLUS_NOTIFICATIONS, {})
    key=f"{did}:{pid}"
    if request.method=="GET":
        lst=data.get(key, [])
        return jsonify({"notified": safe in lst})
    if request.method=="POST":
        lst=data.get(key, [])
        if safe not in lst:
            lst.append(safe)
            data[key]=lst
            _save_json_file(DINGUZPLUS_NOTIFICATIONS, data)
        return jsonify({"ok":True})
    # DELETE
    lst=data.get(key, [])
    if safe in lst:
        lst.remove(safe)
        data[key]=lst
        _save_json_file(DINGUZPLUS_NOTIFICATIONS, data)
    return jsonify({"ok":True})

# ==========================================
#           E-DZIENNIK SZKOLNY - MODUŁ
# ==========================================
import hashlib as _ed_hashlib
EDZ_DATA_FILE = "edziennik_data.json"
EDZ_OFFICE_LINKS = {
    "outlook": "https://outlook.office365.com",
    "word": "https://www.office.com/launch/word",
    "excel": "https://www.office.com/launch/excel",
    "powerpoint": "https://www.office.com/launch/powerpoint",
    "onedrive": "https://onedrive.live.com",
    "teams": "https://teams.microsoft.com",
    "onenote": "https://www.onenote.com"
}
def _ed_hash(p): return _ed_hashlib.sha256(p.encode()).hexdigest()
def _ed_load():
    if os.path.exists(EDZ_DATA_FILE):
        try:
            with open(EDZ_DATA_FILE,"r",encoding="utf-8") as f: return json.load(f)
        except: pass
    return None
def _ed_save(d):
    with open(EDZ_DATA_FILE,"w",encoding="utf-8") as f: json.dump(d,f,indent=2,ensure_ascii=False)
def _ed_seed():
    d=_ed_load()
    if d and d.get("users"): return d
    # seed demo data
    users=[
        {"id":"u1","login":"maciej.kowalski","password":_ed_hash("Test123!"),"name":"Maciej Kowalski","role":"uczen","klasa":"1B","email":"maciej.kowalski@szkola.pl","avatar":"MK","year":2010,"nr":12,"wychowawca":"Katarzyna Maćkowska"},
        {"id":"u2","login":"katarzyna.mackowska","password":_ed_hash("Test123!"),"name":"Katarzyna Maćkowska","role":"wychowawca","klasa":"1B","email":"katarzyna.mackowska@szkola.pl","avatar":"KM"},
        {"id":"u3","login":"anna.nowak","password":_ed_hash("Test123!"),"name":"Anna Nowak","role":"nauczyciel","przedmiot":"Matematyka","avatar":"AN"},
        {"id":"u4","login":"piotr.wisniewski","password":_ed_hash("Test123!"),"name":"Piotr Wiśniewski","role":"nauczyciel","przedmiot":"Informatyka","avatar":"PW"},
        {"id":"u5","login":"marek.zielinski","password":_ed_hash("Test123!"),"name":"Marek Zieliński","role":"nauczyciel","przedmiot":"Język polski","avatar":"MZ"},
        {"id":"u6","login":"joanna.wojcik","password":_ed_hash("Test123!"),"name":"Joanna Wójcik","role":"nauczyciel","przedmiot":"Język angielski","avatar":"JW"},
        {"id":"u7","login":"tomasz.kaminski","password":_ed_hash("Test123!"),"name":"Tomasz Kamiński","role":"nauczyciel","przedmiot":"Fizyka","avatar":"TK"},
        {"id":"u8","login":"admin","password":_ed_hash("Admin123!"),"name":"Administrator","role":"admin","avatar":"AD"},
        {"id":"u9","login":"jan.nowak","password":_ed_hash("Test123!"),"name":"Jan Nowak","role":"uczen","klasa":"1B","avatar":"JN","nr":5},
        {"id":"u10","login":"adam.wisniewski","password":_ed_hash("Test123!"),"name":"Adam Wiśniewski","role":"uczen","klasa":"1B","avatar":"AW","nr":7},
    ]
    classes=[{"id":"1B","name":"1B","wychowawca":"Katarzyna Maćkowska","sala":"204","rok":"2025/2026"}]
    subjects=["Język polski","Matematyka","Język angielski","Informatyka","Fizyka","Chemia","Biologia","Historia","Geografia","WF","Biznes i zarządzanie","Edukacja dla bezpieczeństwa"]
    grades=[]
    import datetime as _dt, random as _rnd
    gid=1
    for subj in subjects:
        for _ in range(_rnd.randint(3,6)):
            val=_rnd.choice(["5","4+","4","3+","5-","2","3","4-","6","5+"])
            grades.append({"id":gid,"student":"maciej.kowalski","subject":subj,"value":val,"weight":_rnd.choice([1,2,3]),"category":_rnd.choice(["Sprawdzian","Kartkówka","Odpowiedź","Aktywność","Projekt","Praca domowa"]),"description":_rnd.choice(["Całkowanie","Lektura","Słownictwo","Algorytmy","Ruch harmoniczny"]),"date":(_dt.date.today()-_dt.timedelta(days=_rnd.randint(1,60))).isoformat(),"teacher":_rnd.choice(["Anna Nowak","Piotr Wiśniewski","Marek Zieliński","Joanna Wójcik","Tomasz Kamiński"])})
            gid+=1
    # extra for Jan, Adam
    for login in ["jan.nowak","adam.wisniewski"]:
        for subj in subjects[:4]:
            grades.append({"id":gid,"student":login,"subject":subj,"value":_rnd.choice(["3","4","5","3+"]),"weight":2,"category":"Sprawdzian","description":"Test","date":_dt.date.today().isoformat(),"teacher":"Anna Nowak"}); gid+=1
    attendance=[]
    for i in range(30):
        d=(_dt.date.today()-_dt.timedelta(days=i)).isoformat()
        if _dt.datetime.strptime(d,"%Y-%m-%d").weekday()>=5: continue
        for login in ["maciej.kowalski","jan.nowak","adam.wisniewski"]:
            status=_rnd.choices(["obecny","nieobecny","spóźniony","zwolniony"],weights=[85,8,5,2])[0]
            attendance.append({"id":len(attendance)+1,"student":login,"date":d,"status":status,"lesson":_rnd.randint(1,7)})
    # plan 1B
    plan=[]
    pid=1
    dni=["Poniedziałek","Wtorek","Środa","Czwartek","Piątek"]
    godziny=["08:00-08:45","08:55-09:40","09:50-10:35","10:45-11:30","11:45-12:30","12:40-13:25","13:35-14:20"]
    nauczyciele={"Język polski":"Marek Zieliński","Matematyka":"Anna Nowak","Język angielski":"Joanna Wójcik","Informatyka":"Piotr Wiśniewski","Fizyka":"Tomasz Kamiński","Chemia":"Anna Nowak","Biologia":"Katarzyna Maćkowska","Historia":"Marek Zieliński","Geografia":"Joanna Wójcik","WF":"Tomasz Kamiński","Biznes i zarządzanie":"Katarzyna Maćkowska","Edukacja dla bezpieczeństwa":"Piotr Wiśniewski"}
    for d_idx, dzien in enumerate(dni):
        for h_idx, godz in enumerate(godziny):
            if _rnd.random()<0.15: continue
            subj=_rnd.choice(subjects)
            plan.append({"id":pid,"klasa":"1B","dzien":dzien,"godzina":godz,"przedmiot":subj,"nauczyciel":nauczyciele.get(subj,"Anna Nowak"),"sala":str(200+_rnd.randint(1,15)),"odwolana":False,"zastepstwo":None,"temat":""})
            pid+=1
    messages=[
        {"id":1,"from":"Katarzyna Maćkowska","from_login":"katarzyna.mackowska","to":"maciej.kowalski","subject":"Zebranie klasowe","body":"Przypominam o zebraniu w piątek 17:00 sala 204.","date":(_dt.datetime.now()-_dt.timedelta(days=1)).isoformat(),"read":False,"folder":"odebrane"},
        {"id":2,"from":"Anna Nowak","from_login":"anna.nowak","to":"maciej.kowalski","subject":"Poprawa sprawdzianu","body":"Możliwość poprawy w czwartek po lekcjach.","date":(_dt.datetime.now()-_dt.timedelta(days=2)).isoformat(),"read":False,"folder":"odebrane"},
        {"id":3,"from":"Maciej Kowalski","from_login":"maciej.kowalski","to":"katarzyna.mackowska","subject":"Nieobecność","body":"Będę nieobecny jutro - wizyta lekarska.","date":(_dt.datetime.now()-_dt.timedelta(days=3)).isoformat(),"read":True,"folder":"wyslane"},
    ]
    notes=[{"id":1,"student":"maciej.kowalski","teacher":"Marek Zieliński","type":"uwaga","content":"Spóźnienie na lekcję","date":(_dt.date.today()-_dt.timedelta(days=5)).isoformat()},{"id":2,"student":"maciej.kowalski","teacher":"Anna Nowak","type":"pochwala","content":"Aktywność na lekcji matematyki","date":(_dt.date.today()-_dt.timedelta(days=2)).isoformat()}]
    textbooks=[{"id":1,"przedmiot":"Matematyka","tytul":"Matematyka 1","autor":"Kurczab","wydawnictwo":"OE","isbn":"978-83-12345-67-8","klasa":"1B","rok":"2025/2026"},{"id":2,"przedmiot":"Język polski","tytul":"Ponad słowami 1","autor":"Nowak","wydawnictwo":"Nowa Era","isbn":"978-83-87654-32-1","klasa":"1B","rok":"2025/2026"}]
    events=[{"id":1,"title":"Sprawdzian z matematyki","date":(_dt.date.today()+_dt.timedelta(days=3)).isoformat(),"type":"sprawdzian","klasa":"1B","description":"Dział: funkcje"},{"id":2,"title":"Wycieczka do muzeum","date":(_dt.date.today()+_dt.timedelta(days=10)).isoformat(),"type":"wycieczka","klasa":"1B","description":"Muzeum Narodowe"},{"id":3,"title":"Dzień wolny","date":(_dt.date.today()+_dt.timedelta(days=15)).isoformat(),"type":"wolne","klasa":"1B","description":"Święto szkoły"}]
    rooms=[{"id":"204","name":"204","budynek":"A"},{"id":"105","name":"105","budynek":"A"}]
    data={"users":users,"classes":classes,"subjects":subjects,"grades":grades,"attendance":attendance,"plan":plan,"messages":messages,"notes":notes,"textbooks":textbooks,"events":events,"rooms":rooms,"logs":[],"office_links":EDZ_OFFICE_LINKS}
    _ed_save(data)
    return data
def _ed_get():
    d=_ed_load()
    if not d: d=_ed_seed()
    return d
def _ed_current():
    return session.get("edziennik_user")
def _ed_require(role=None):
    u=_ed_current()
    if not u: return None
    if role:
        allowed = role if isinstance(role, list) else [role]
        if u.get("role") not in allowed and u.get("role")!="admin": return None
    return u

@app.route("/edziennik")
def edziennik_page():
    return render_template("edziennik.html")

@app.route("/api/edziennik/login", methods=["POST"])
def edz_login():
    j=request.get_json() or {}
    login=(j.get("login") or "").strip()
    pwd=j.get("password") or ""
    d=_ed_get()
    user=next((u for u in d["users"] if u["login"]==login), None)
    if not user or user["password"]!=_ed_hash(pwd):
        return jsonify({"error":"Błędny login lub hasło"}),401
    session["edziennik_user"]={k:v for k,v in user.items() if k!="password"}
    session.permanent=True
    d.setdefault("logs",[]).append({"user":login,"action":"login","date":datetime.datetime.now().isoformat(),"details":""})
    _ed_save(d)
    return jsonify(session["edziennik_user"])

@app.route("/api/edziennik/logout", methods=["POST"])
def edz_logout():
    session.pop("edziennik_user",None)
    return jsonify({"ok":True})

@app.route("/api/edziennik/me")
def edz_me():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    return jsonify(u)

@app.route("/api/edziennik/dashboard")
def edz_dashboard():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    # karty dla ucznia
    if u["role"]=="uczen":
        grades=[g for g in d["grades"] if g["student"]==u["login"]]
        att=[a for a in d["attendance"] if a["student"]==u["login"]]
        notes=[n for n in d["notes"] if n["student"]==u["login"]]
        msgs=[m for m in d["messages"] if m["to"]==u["login"] and not m.get("read")]
        def avg():
            vals=[]
            for g in grades:
                try:
                    v=g["value"].replace("+",".5").replace("-","-.25")
                    # 4+ =4.5 etc, uproszczone
                    if "+" in g["value"]: vals.append(float(g["value"][0])+0.5)
                    elif "-" in g["value"]: vals.append(float(g["value"][0])-0.25)
                    else: vals.append(float(g["value"]))
                except: pass
            return round(sum(vals)/len(vals),2) if vals else 0
        return jsonify({
            "avg":avg(),
            "frekwencja": round(len([a for a in att if a["status"]=="obecny"])/len(att)*100,1) if att else 100,
            "liczba_ocen":len(grades),
            "nieobecnosci":len([a for a in att if a["status"]=="nieobecny"]),
            "uwagi":len([n for n in notes if n["type"]=="uwaga"]),
            "pochwaly":len([n for n in notes if n["type"]=="pochwala"]),
            "nieprzeczytane":len(msgs),
            "plan_dzis": [p for p in d["plan"] if p["klasa"]==u.get("klasa") and p["dzien"]==["Poniedziałek","Wtorek","Środa","Czwartek","Piątek","Sobota","Niedziela"][datetime.datetime.now().weekday()] ][:7] if datetime.datetime.now().weekday()<5 else [],
            "ostatnie_oceny": sorted(grades, key=lambda x:x["date"], reverse=True)[:5],
            "wiadomosci": [m for m in d["messages"] if m["to"]==u["login"]][:3],
            "ogloszenia": d["events"][:3]
        })
    else:
        # wychowawca/admin
        klasa=u.get("klasa","1B")
        uczniowie=[x for x in d["users"] if x.get("klasa")==klasa and x["role"]=="uczen"]
        return jsonify({"uczniowie":len(uczniowie),"klasa":klasa,"plan":d["plan"][:5]})

@app.route("/api/edziennik/grades")
def edz_grades():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    subj=request.args.get("subject")
    login=request.args.get("student") or (u["login"] if u["role"]=="uczen" else None)
    grades=d["grades"]
    if login: grades=[g for g in grades if g["student"]==login]
    if subj: grades=[g for g in grades if g["subject"]==subj]
    return jsonify(grades)

@app.route("/api/edziennik/grades", methods=["POST"])
def edz_add_grade():
    u=_ed_current()
    if not u or u["role"] not in ["nauczyciel","wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    j=request.get_json() or {}
    d=_ed_get()
    nid=max([g["id"] for g in d["grades"]], default=0)+1
    g={"id":nid,"student":j.get("student"),"subject":j.get("subject"),"value":str(j.get("value")),"weight":int(j.get("weight",1)),"category":j.get("category","Sprawdzian"),"description":j.get("description",""),"date":j.get("date") or datetime.date.today().isoformat(),"teacher":u["name"]}
    d["grades"].append(g)
    d.setdefault("logs",[]).append({"user":u["login"],"action":"add_grade","date":datetime.datetime.now().isoformat(),"details":str(g)})
    _ed_save(d)
    return jsonify(g)

@app.route("/api/edziennik/grades/<int:gid>", methods=["PUT","DELETE"])
def edz_mod_grade(gid):
    u=_ed_current()
    if not u or u["role"] not in ["nauczyciel","wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    d=_ed_get()
    g=next((x for x in d["grades"] if x["id"]==gid),None)
    if not g: return jsonify({"error":"not found"}),404
    if request.method=="DELETE":
        d["grades"]=[x for x in d["grades"] if x["id"]!=gid]
        d.setdefault("logs",[]).append({"user":u["login"],"action":"delete_grade","date":datetime.datetime.now().isoformat(),"details":str(gid)})
        _ed_save(d)
        return jsonify({"ok":True})
    j=request.get_json() or {}
    for k in ["value","weight","category","description","subject","student"]:
        if k in j: g[k]=j[k]
    d["logs"].append({"user":u["login"],"action":"edit_grade","date":datetime.datetime.now().isoformat(),"details":str(gid)})
    _ed_save(d)
    return jsonify(g)

@app.route("/api/edziennik/grades/bulk", methods=["POST"])
def edz_bulk_grades():
    u=_ed_current()
    if not u or u["role"] not in ["nauczyciel","wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    j=request.get_json() or {}
    items=j.get("items") or []
    d=_ed_get()
    res=[]
    for it in items:
        nid=max([g["id"] for g in d["grades"]], default=0)+1
        g={"id":nid,"student":it.get("student"),"subject":it.get("subject"),"value":str(it.get("value")),"weight":int(it.get("weight",1)),"category":it.get("category","Sprawdzian"),"description":it.get("description",""),"date":datetime.date.today().isoformat(),"teacher":u["name"]}
        d["grades"].append(g); res.append(g)
    _ed_save(d)
    return jsonify(res)

@app.route("/api/edziennik/attendance")
def edz_att():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    login=request.args.get("student") or (u["login"] if u["role"]=="uczen" else None)
    # klasa param for wychowawca
    klasa=request.args.get("klasa")
    att=d["attendance"]
    if login: att=[a for a in att if a["student"]==login]
    if klasa:
        ucz=[x["login"] for x in d["users"] if x.get("klasa")==klasa]
        att=[a for a in att if a["student"] in ucz]
    return jsonify(att)

@app.route("/api/edziennik/attendance", methods=["POST"])
def edz_att_post():
    u=_ed_current()
    if not u or u["role"] not in ["nauczyciel","wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    j=request.get_json() or {}
    d=_ed_get()
    # j: {student, date, status, lesson}
    nid=max([a["id"] for a in d["attendance"]], default=0)+1
    a={"id":nid,"student":j.get("student"),"date":j.get("date"),"status":j.get("status","obecny"),"lesson":j.get("lesson",1)}
    # upsert
    existing=next((x for x in d["attendance"] if x["student"]==a["student"] and x["date"]==a["date"] and x["lesson"]==a["lesson"]),None)
    if existing:
        existing.update(a); a=existing
    else: d["attendance"].append(a)
    _ed_save(d)
    return jsonify(a)

@app.route("/api/edziennik/schedule")
def edz_schedule():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    klasa=request.args.get("klasa") or u.get("klasa") or "1B"
    plan=[p for p in d["plan"] if p["klasa"]==klasa]
    return jsonify(plan)

@app.route("/api/edziennik/schedule", methods=["POST"])
def edz_schedule_post():
    u=_ed_current()
    if not u or u["role"] not in ["wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    j=request.get_json() or {}
    d=_ed_get()
    nid=max([p["id"] for p in d["plan"]], default=0)+1
    p={"id":nid,"klasa":j.get("klasa","1B"),"dzien":j.get("dzien","Poniedziałek"),"godzina":j.get("godzina","08:00-08:45"),"przedmiot":j.get("przedmiot"),"nauczyciel":j.get("nauczyciel"),"sala":j.get("sala"),"odwolana":False,"zastepstwo":None,"temat":j.get("temat","")}
    d["plan"].append(p)
    d.setdefault("logs",[]).append({"user":u["login"],"action":"add_lesson","date":datetime.datetime.now().isoformat(),"details":str(p)})
    _ed_save(d)
    return jsonify(p)

@app.route("/api/edziennik/schedule/<int:pid>", methods=["PUT","DELETE"])
def edz_schedule_mod(pid):
    u=_ed_current()
    if not u or u["role"] not in ["wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    d=_ed_get()
    p=next((x for x in d["plan"] if x["id"]==pid),None)
    if not p: return jsonify({"error":"not found"}),404
    if request.method=="DELETE":
        d["plan"]=[x for x in d["plan"] if x["id"]!=pid]
        d["logs"].append({"user":u["login"],"action":"delete_lesson","date":datetime.datetime.now().isoformat(),"details":str(pid)})
        _ed_save(d); return jsonify({"ok":True})
    j=request.get_json() or {}
    for k in ["dzien","godzina","przedmiot","nauczyciel","sala","temat","odwolana","zastepstwo"]:
        if k in j: p[k]=j[k]
    d["logs"].append({"user":u["login"],"action":"edit_lesson","date":datetime.datetime.now().isoformat(),"details":str(pid)})
    _ed_save(d); return jsonify(p)

@app.route("/api/edziennik/schedule/cancel/<int:pid>", methods=["POST"])
def edz_cancel(pid):
    u=_ed_current()
    if not u or u["role"] not in ["wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    j=request.get_json() or {}
    d=_ed_get()
    p=next((x for x in d["plan"] if x["id"]==pid),None)
    if not p: return jsonify({"error":"not found"}),404
    p["odwolana"]=not p.get("odwolana",False)
    if p["odwolana"]: p["powod"]=j.get("powod","")
    d["logs"].append({"user":u["login"],"action":"cancel_lesson","date":datetime.datetime.now().isoformat(),"details":str(pid)})
    _ed_save(d); return jsonify(p)

@app.route("/api/edziennik/substitutions", methods=["GET","POST"])
def edz_sub():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    if request.method=="GET":
        return jsonify([p for p in d["plan"] if p.get("zastepstwo")])
    if u["role"] not in ["wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    j=request.get_json() or {}
    pid=j.get("lesson_id")
    p=next((x for x in d["plan"] if x["id"]==pid),None)
    if not p: return jsonify({"error":"not found"}),404
    p["zastepstwo"]={"nauczyciel":j.get("nauczyciel"),"sala":j.get("sala"),"komentarz":j.get("komentarz","")}
    _ed_save(d); return jsonify(p)

@app.route("/api/edziennik/messages")
def edz_msgs():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    folder=request.args.get("folder","odebrane")
    login=u["login"]
    if folder=="odebrane": msgs=[m for m in d["messages"] if m["to"]==login]
    elif folder=="wyslane": msgs=[m for m in d["messages"] if m["from_login"]==login]
    else: msgs=[m for m in d["messages"] if m["to"]==login or m["from_login"]==login]
    # search
    q=request.args.get("q","").lower()
    if q: msgs=[m for m in msgs if q in m["subject"].lower() or q in m["body"].lower()]
    return jsonify(sorted(msgs, key=lambda x:x["date"], reverse=True))

@app.route("/api/edziennik/messages", methods=["POST"])
def edz_msg_post():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    j=request.get_json() or {}
    d=_ed_get()
    nid=max([m["id"] for m in d["messages"]], default=0)+1
    # obsługa wysłania do klasy
    recipients=j.get("to")
    if recipients=="klasa:1B":
        ucz=[x["login"] for x in d["users"] if x.get("klasa")=="1B" and x["role"]=="uczen"]
        for r in ucz:
            nid+=1
            d["messages"].append({"id":nid,"from":u["name"],"from_login":u["login"],"to":r,"subject":j.get("subject"),"body":j.get("body"),"date":datetime.datetime.now().isoformat(),"read":False,"folder":"odebrane"})
        _ed_save(d); return jsonify({"ok":True})
    m={"id":nid,"from":u["name"],"from_login":u["login"],"to":recipients,"subject":j.get("subject"),"body":j.get("body"),"date":datetime.datetime.now().isoformat(),"read":False,"folder":"odebrane"}
    d["messages"].append(m); _ed_save(d); return jsonify(m)

@app.route("/api/edziennik/messages/<int:mid>", methods=["PUT","DELETE"])
def edz_msg_mod(mid):
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    m=next((x for x in d["messages"] if x["id"]==mid),None)
    if not m: return jsonify({"error":"not found"}),404
    if request.method=="DELETE":
        d["messages"]=[x for x in d["messages"] if x["id"]!=mid]; _ed_save(d); return jsonify({"ok":True})
    j=request.get_json() or {}
    if "read" in j: m["read"]=bool(j["read"])
    _ed_save(d); return jsonify(m)

@app.route("/api/edziennik/notes")
def edz_notes():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    login=request.args.get("student") or (u["login"] if u["role"]=="uczen" else None)
    notes=d["notes"]
    if login: notes=[n for n in notes if n["student"]==login]
    typ=request.args.get("type")
    if typ: notes=[n for n in notes if n["type"]==typ]
    return jsonify(notes)

@app.route("/api/edziennik/notes", methods=["POST"])
def edz_notes_post():
    u=_ed_current()
    if not u or u["role"] not in ["nauczyciel","wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    j=request.get_json() or {}
    d=_ed_get()
    nid=max([n["id"] for n in d["notes"]], default=0)+1
    n={"id":nid,"student":j.get("student"),"teacher":u["name"],"type":j.get("type","uwaga"),"content":j.get("content"),"date":datetime.date.today().isoformat()}
    d["notes"].append(n)
    d.setdefault("logs",[]).append({"user":u["login"],"action":"add_note","date":datetime.datetime.now().isoformat(),"details":str(n)})
    _ed_save(d); return jsonify(n)

@app.route("/api/edziennik/textbooks", methods=["GET","POST"])
def edz_books():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    if request.method=="GET": return jsonify(d["textbooks"])
    if u["role"] not in ["wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    j=request.get_json() or {}
    nid=max([b["id"] for b in d["textbooks"]], default=0)+1
    b={"id":nid,"przedmiot":j.get("przedmiot"),"tytul":j.get("tytul"),"autor":j.get("autor"),"wydawnictwo":j.get("wydawnictwo"),"isbn":j.get("isbn"),"klasa":j.get("klasa","1B"),"rok":j.get("rok","2025/2026")}
    d["textbooks"].append(b); _ed_save(d); return jsonify(b)

@app.route("/api/edziennik/textbooks/<int:bid>", methods=["PUT","DELETE"])
def edz_book_mod(bid):
    u=_ed_current()
    if not u or u["role"] not in ["wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    d=_ed_get()
    b=next((x for x in d["textbooks"] if x["id"]==bid),None)
    if not b: return jsonify({"error":"not found"}),404
    if request.method=="DELETE":
        d["textbooks"]=[x for x in d["textbooks"] if x["id"]!=bid]; _ed_save(d); return jsonify({"ok":True})
    j=request.get_json() or {}
    for k in ["przedmiot","tytul","autor","wydawnictwo","isbn","klasa","rok"]:
        if k in j: b[k]=j[k]
    _ed_save(d); return jsonify(b)

@app.route("/api/edziennik/events", methods=["GET","POST"])
def edz_events():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    if request.method=="GET": return jsonify(d["events"])
    if u["role"] not in ["wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    j=request.get_json() or {}
    nid=max([e["id"] for e in d["events"]], default=0)+1
    e={"id":nid,"title":j.get("title"),"date":j.get("date"),"type":j.get("type","inne"),"klasa":j.get("klasa","1B"),"description":j.get("description","")}
    d["events"].append(e); _ed_save(d); return jsonify(e)

@app.route("/api/edziennik/events/<int:eid>", methods=["DELETE"])
def edz_event_del(eid):
    u=_ed_current()
    if not u or u["role"] not in ["wychowawca","admin"]: return jsonify({"error":"forbidden"}),403
    d=_ed_get(); d["events"]=[x for x in d["events"] if x["id"]!=eid]; _ed_save(d); return jsonify({"ok":True})

@app.route("/api/edziennik/class/students")
def edz_class_students():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    klasa=request.args.get("klasa") or u.get("klasa") or "1B"
    stud=[x for x in d["users"] if x.get("klasa")==klasa and x["role"]=="uczen"]
    # dopisz średnie/frekwencja
    for s in stud:
        grades=[g for g in d["grades"] if g["student"]==s["login"]]
        vals=[]
        for g in grades:
            try:
                if "+" in g["value"]: vals.append(float(g["value"][0])+0.5)
                elif "-" in g["value"]: vals.append(float(g["value"][0])-0.25)
                else: vals.append(float(g["value"]))
            except: pass
        s["avg"]=round(sum(vals)/len(vals),2) if vals else 0
        att=[a for a in d["attendance"] if a["student"]==s["login"]]
        s["frekw"]=round(len([a for a in att if a["status"]=="obecny"])/len(att)*100,1) if att else 100
    return jsonify(stud)

@app.route("/api/edziennik/office")
def edz_office():
    d=_ed_get()
    return jsonify(d.get("office_links",EDZ_OFFICE_LINKS))

@app.route("/api/edziennik/search")
def edz_search():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    q=(request.args.get("q") or "").lower()
    if not q: return jsonify([])
    d=_ed_get()
    res=[]
    for user in d["users"]:
        if q in user["name"].lower() or q in user["login"]:
            res.append({"type":"user","label":user["name"]+" ("+user["role"]+")","id":user["login"]})
    for subj in d["subjects"]:
        if q in subj.lower(): res.append({"type":"przedmiot","label":subj,"id":subj})
    for m in d["messages"]:
        if q in m["subject"].lower(): res.append({"type":"wiadomość","label":m["subject"],"id":m["id"]})
    return jsonify(res[:10])

@app.route("/api/edziennik/notifications")
def edz_notifs():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    # proste: nieprzeczytane wiadomości + nowe oceny z ostatnich 3 dni
    msgs=[m for m in d["messages"] if m["to"]==u["login"] and not m.get("read")]
    recent_grades=[g for g in d["grades"] if g["student"]==u["login"] and g["date"]>= (datetime.date.today()-datetime.timedelta(days=3)).isoformat()]
    notifs=[]
    for m in msgs: notifs.append({"id":"m"+str(m["id"]),"text":"Nowa wiadomość: "+m["subject"],"date":m["date"]})
    for g in recent_grades: notifs.append({"id":"g"+str(g["id"]),"text":f"Nowa ocena {g['value']} z {g['subject']}","date":g["date"]})
    plan_cancelled=[p for p in d["plan"] if p.get("odwolana") and p["klasa"]==u.get("klasa")]
    for p in plan_cancelled[:2]: notifs.append({"id":"p"+str(p["id"]),"text":f"Odwołana lekcja {p['przedmiot']} {p['dzien']}","date":datetime.date.today().isoformat()})
    return jsonify(notifs[:10])

@app.route("/api/edziennik/settings", methods=["GET","POST"])
def edz_settings():
    u=_ed_current()
    if not u: return jsonify({"error":"unauthorized"}),401
    d=_ed_get()
    if request.method=="GET":
        return jsonify({"user":u,"office_links":d.get("office_links",EDZ_OFFICE_LINKS)})
    j=request.get_json() or {}
    # zmiana hasła
    if j.get("new_password"):
        if _ed_hash(j.get("old_password","")) != next((x["password"] for x in d["users"] if x["login"]==u["login"]), ""):
            return jsonify({"error":"Błędne stare hasło"}),400
        for user in d["users"]:
            if user["login"]==u["login"]:
                user["password"]=_ed_hash(j["new_password"])
                session["edziennik_user"]={k:v for k,v in user.items() if k!="password"}
                break
        _ed_save(d); return jsonify({"ok":True})
    # office links admin
    if j.get("office_links") and u["role"]=="admin":
        d["office_links"]=j["office_links"]; _ed_save(d); return jsonify({"ok":True})
    return jsonify({"ok":True})

@app.route("/api/edziennik/admin/users", methods=["GET","POST"])
def edz_admin_users():
    u=_ed_current()
    if not u or u["role"]!="admin": return jsonify({"error":"forbidden"}),403
    d=_ed_get()
    if request.method=="GET": return jsonify([{k:v for k,v in x.items() if k!="password"} for x in d["users"]])
    j=request.get_json() or {}
    nid="u"+str(len(d["users"])+1)
    nu={"id":nid,"login":j.get("login"),"password":_ed_hash(j.get("password","Test123!")),"name":j.get("name"),"role":j.get("role","uczen"),"klasa":j.get("klasa"),"avatar":j.get("name","?")[:2].upper()}
    d["users"].append(nu); _ed_save(d); return jsonify({k:v for k,v in nu.items() if k!="password"})

@app.route("/api/edziennik/admin/users/<login>", methods=["PUT","DELETE"])
def edz_admin_user_mod(login):
    u=_ed_current()
    if not u or u["role"]!="admin": return jsonify({"error":"forbidden"}),403
    d=_ed_get()
    user=next((x for x in d["users"] if x["login"]==login),None)
    if not user: return jsonify({"error":"not found"}),404
    if request.method=="DELETE":
        d["users"]=[x for x in d["users"] if x["login"]!=login]; _ed_save(d); return jsonify({"ok":True})
    j=request.get_json() or {}
    for k in ["name","role","klasa","email"]:
        if k in j: user[k]=j[k]
    if j.get("password"): user["password"]=_ed_hash(j["password"])
    _ed_save(d); return jsonify({k:v for k,v in user.items() if k!="password"})

@app.route("/api/edziennik/logs")
def edz_logs():
    u=_ed_current()
    if not u or u["role"]!="admin": return jsonify({"error":"forbidden"}),403
    d=_ed_get()
    return jsonify(d.get("logs",[])[-50:][::-1])

# ==========================================
#               URUCHOMIENIE
# ==========================================

if __name__ == "__main__":
    port = int(os.environ.get("SERVER_PORT", 5000))
    app.run(port=port, host="0.0.0.0")
