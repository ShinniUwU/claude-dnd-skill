"""auth_ext.py — Discord login, per-player access and per-character privacy
for the hosted (velvetcore) fork of the DnD display.

How it fits together
--------------------
* Browsers log in with Discord (OAuth2 "identify" scope, stdlib HTTP only).
  Only Discord user IDs listed in access.json may enter. The owner
  (DND_OWNER_ID) can add/remove people and assign characters on /settings.
* Each person is bound to ONE character. They can only stage actions as that
  character, plus "Everybody" while the party is together.
* The DM's scripts (send.py, push_stats.py, ...) keep using the X-DND-Token
  header. Script-only endpoints refuse browser sessions, so a logged-in player
  can never post narration or drain someone else's queued actions.
* Text blocks can carry "to": [character names]. Those are only delivered to
  viewers bound to one of those characters. While the party is split, player
  echoes and dice lines that start with a character name are private to that
  character automatically, even if the DM forgot --to.

Config (environment, usually from /opt/dnd/app.env):
  DND_OWNER_ID           Discord user id of the owner (always allowed, can't be removed)
  DISCORD_CLIENT_ID      Discord application client id
  DISCORD_CLIENT_SECRET  Discord application client secret
  DND_PUBLIC_URL         e.g. https://dnd.shinni.dev (used for the OAuth redirect)
  DND_SESSION_SECRET     random string used to sign session cookies
"""
from __future__ import annotations

import copy
import hmac
import json
import os
import re
import secrets
import threading
import urllib.parse
import urllib.request
from datetime import timedelta

from flask import (Response, abort, g, jsonify, redirect, render_template,
                   request, session)

OWNER_ID = os.environ.get("DND_OWNER_ID", "").strip()
CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
PUBLIC_URL = os.environ.get("DND_PUBLIC_URL", "").strip().rstrip("/")
REDIRECT_URI = f"{PUBLIC_URL}/auth/callback"

_DATA_ROOT = os.path.expanduser(os.environ.get("DND_CAMPAIGN_ROOT", "~/.claude/dnd"))
ACCESS_FILE = os.path.join(_DATA_ROOT, "access.json")
_access_lock = threading.Lock()

_ID_RE = re.compile(r"^\d{15,22}$")
_NAME_RE = re.compile(r"^[\w][\w '\-]{0,48}$", re.UNICODE)

# Endpoints only the DM's scripts may call (token required, sessions refused).
SCRIPT_ONLY = {
    "/chunk", "/stats", "/clear", "/dice-request", "/queue/consumed",
    "/player-input/drain", "/player-input/submit-now",
    "/device/approve", "/device/deny",
}
SCRIPT_ONLY_PREFIX = ("/dice-request/",)
# Endpoints anyone may reach without logging in.
PUBLIC = {"/login", "/auth/callback", "/logout", "/ping", "/health", "/favicon.ico"}
PUBLIC_PREFIX = ("/icons/",)
# Endpoints whose JSON body names a character that must be the caller's own.
# Display settings each person can save to their account (browser localStorage keys).
PREF_KEYS = {
    "dnd-theme", "dnd-skin", "dnd-text-scale", "dnd-narration-words", "dnd-onscreen-dice",
    "dnd_tts_auto", "dnd_tts_voice", "dnd-type-speed", "dnd-sfx", "dnd-controls-visible",
}
CHAR_BOUND = {
    "/player-input", "/player-input/stage", "/player-input/ready",
    "/player-input/unstage", "/player-input/skip", "/player-input/dice",
}

_token_getter = None   # set by init(): returns the app's token (or None)
on_party_change = None  # optional callback(together: bool), set by the app


# ─── access.json ──────────────────────────────────────────────────────────────

def _default_access() -> dict:
    users = {}
    if OWNER_ID:
        users[OWNER_ID] = {"name": "Owner", "character": ""}
    return {"together": True, "users": users}


def load_access() -> dict:
    with _access_lock:
        try:
            with open(ACCESS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = _default_access()
        data.setdefault("together", True)
        data.setdefault("users", {})
        if OWNER_ID and OWNER_ID not in data["users"]:
            data["users"][OWNER_ID] = {"name": "Owner", "character": ""}
        return data


def save_access(data: dict) -> None:
    with _access_lock:
        tmp = ACCESS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, ACCESS_FILE)


def is_together() -> bool:
    return bool(load_access().get("together", True))


def current_user() -> "dict | None":
    """The logged-in, still-authorised user, or None. Re-checked every request
    so removing someone on /settings locks them out immediately."""
    uid = session.get("uid")
    if not uid:
        return None
    entry = load_access()["users"].get(uid)
    if not entry:
        return None
    return {
        "id": uid,
        "name": entry.get("name") or session.get("uname") or uid,
        "character": entry.get("character", ""),
        "owner": uid == OWNER_ID,
        "discord_name": session.get("uname", ""),
    }


def me_payload() -> dict:
    u = current_user()
    if not u:
        return {}
    prefs = load_access()["users"].get(u["id"], {}).get("prefs", {})
    return {
        "name": u["name"],
        "character": u["character"],
        "owner": u["owner"],
        "together": is_together(),
        "prefs": {k: v for k, v in prefs.items() if k in PREF_KEYS},
    }


# ─── Request gate ─────────────────────────────────────────────────────────────

def _token_valid() -> bool:
    tok = _token_getter() if _token_getter else None
    if not tok:
        return False
    return hmac.compare_digest(request.headers.get("X-DND-Token", ""), tok)


def _is_script_only(path: str) -> bool:
    return path in SCRIPT_ONLY or path.startswith(SCRIPT_ONLY_PREFIX)


def before_request():
    path = request.path
    g.dnd_user = None
    if path in PUBLIC or path.startswith(PUBLIC_PREFIX):
        return None
    if _is_script_only(path):
        if not _token_valid():
            return "Forbidden", 403
        return None
    if _token_valid():
        return None          # DM scripts may also read browser endpoints
    user = current_user()
    if not user:
        if request.method == "GET" and path == "/":
            return redirect("/login")
        return "Login required", 401
    g.dnd_user = user
    # Same-origin check for state-changing browser calls (CSRF backstop on top
    # of the SameSite=Lax session cookie).
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("Origin", "")
        if origin and PUBLIC_URL and origin.rstrip("/") != PUBLIC_URL:
            return "Bad origin", 403
    if path in CHAR_BOUND and request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        char = str(data.get("character", "")).strip()
        own = (user["character"] or "").strip()
        if not own:
            return "No character assigned to you yet. Ask the owner.", 403
        if char.lower() == own.lower():
            return None
        if char == "Everybody" and is_together():
            return None
        return "You can only act as your own character.", 403
    return None


def token_ok() -> bool:
    """Replacement for the app's _token_ok(): a logged-in browser counts as
    authorised for browser endpoints; script-only endpoints need the token."""
    if _is_script_only(request.path):
        return _token_valid()
    return bool(getattr(g, "dnd_user", None)) or _token_valid()


def viewer_char() -> str:
    u = getattr(g, "dnd_user", None)
    return ((u or {}).get("character") or "").strip().lower()


# ─── Privacy filtering ────────────────────────────────────────────────────────

def _allowed(entry_to, viewer: str) -> bool:
    if not entry_to:
        return True
    return viewer in {str(t).strip().lower() for t in entry_to}


def filter_for(payload: dict, viewer: "str | None") -> "dict | None":
    """Return the payload this viewer may see, or None to skip it entirely."""
    viewer = (viewer or "").lower()
    if "to" in payload and not _allowed(payload.get("to"), viewer):
        return None
    out = payload
    if "replay_batch" in payload:
        out = dict(out)
        out["replay_batch"] = [e for e in payload["replay_batch"]
                               if _allowed(e.get("to"), viewer)]
    if isinstance(payload.get("staged_inputs"), dict):
        out = dict(out)
        out["staged_inputs"] = {k: v for k, v in payload["staged_inputs"].items()
                                if k.lower() == viewer or k == "Everybody"}
    if isinstance(payload.get("pending_input"), list):
        out = dict(out)
        out["pending_input"] = [e for e in payload["pending_input"]
                                if str(e.get("character", "")).lower() in (viewer, "everybody")]
    # queue_status (who has submitted) is names only and stays visible to all,
    # so players can see when everyone is ready.
    return out


def audience(data: dict, text: str, known_players: "list[str]") -> "list[str] | None":
    """Who may see a /chunk. Explicit "to" wins. While the party is split,
    player echoes and dice lines for a known character default to private."""
    to = data.get("to")
    if isinstance(to, list):
        clean = [str(t).strip()[:60] for t in to if str(t).strip()]
        return clean or None
    if is_together():
        return None
    if data.get("player"):
        name = str(data["player"]).strip()
        if name and name != "Everybody":
            return [name]
    if data.get("dice"):
        for p in known_players:
            if text.strip().lower().startswith(p.lower()):
                return [p]
    return None


# ─── Discord OAuth ────────────────────────────────────────────────────────────

def _http_json(url: str, data: "dict | None" = None, headers: "dict | None" = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": "dnd-display (velvetcore, 1.0)",
        **({"Content-Type": "application/x-www-form-urlencoded"} if body else {}),
        **(headers or {}),
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def init(app, token_getter, broadcast, characters_getter) -> None:
    """Wire auth into the Flask app. Call once, after the app is created."""
    global _token_getter
    _token_getter = token_getter

    secret = os.environ.get("DND_SESSION_SECRET", "").strip()
    app.secret_key = secret or secrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=PUBLIC_URL.startswith("https://"),
        SESSION_COOKIE_NAME="dnd_session",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    app.before_request(before_request)

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        # Pages change often; never let a browser show a stale copy.
        if request.path in ("/", "/settings", "/login"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/login")
    def login():
        if current_user():
            return redirect("/")
        if not (CLIENT_ID and CLIENT_SECRET and PUBLIC_URL):
            return render_template("login.html", error="Discord login isn't configured yet (app.env).")
        state = secrets.token_urlsafe(24)
        session["oauth_state"] = state
        url = "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "identify",
            "state": state,
            "prompt": "none",
        })
        if request.args.get("go"):
            return redirect(url)
        return render_template("login.html", error=request.args.get("error", ""), login_url="/login?go=1")

    @app.route("/auth/callback")
    def auth_callback():
        state = session.pop("oauth_state", None)
        if not state or not hmac.compare_digest(state, request.args.get("state", "")):
            return redirect("/login?error=Login+expired,+try+again.")
        code = request.args.get("code", "")
        if not code:
            return redirect("/login?error=Discord+login+was+cancelled.")
        try:
            tok = _http_json("https://discord.com/api/oauth2/token", data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            })
            user = _http_json("https://discord.com/api/users/@me", headers={
                "Authorization": f"Bearer {tok['access_token']}"})
        except Exception:
            return redirect("/login?error=Could+not+reach+Discord.")
        uid = str(user.get("id", ""))
        if uid not in load_access()["users"]:
            session.clear()
            return render_template("login.html",
                                   error=f"Your Discord account ({uid}) isn't on the guest list. "
                                         f"Send this ID to the owner.")
        session.clear()
        session.permanent = True
        session["uid"] = uid
        session["uname"] = str(user.get("global_name") or user.get("username") or "")[:60]
        return redirect("/")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.route("/me")
    def me():
        return jsonify(me_payload())

    @app.route("/party-mode", methods=["POST"])
    def party_mode():
        # Only the DM (its scripts, with the token) decides whether the party
        # is together or split, based on what happens in the story.
        if not _token_valid():
            return "Forbidden", 403
        data = request.get_json(force=True, silent=True) or {}
        acc = load_access()
        acc["together"] = bool(data.get("together", True))
        save_access(acc)
        broadcast({"party_mode": {"together": acc["together"]}})
        if on_party_change:
            on_party_change(acc["together"])
        return jsonify({"together": acc["together"]})

    @app.route("/prefs", methods=["POST"])
    def save_prefs():
        user = getattr(g, "dnd_user", None)
        if not user:
            return "Login required", 401
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return "Bad Request", 400
        clean = {str(k): str(v)[:40] for k, v in data.items() if k in PREF_KEYS}
        acc = load_access()
        entry = acc["users"].get(user["id"])
        if entry is None:
            return "Login required", 401
        prefs = entry.setdefault("prefs", {})
        prefs.update(clean)
        save_access(acc)
        return "", 204

    @app.route("/settings")
    def settings_page():
        user = getattr(g, "dnd_user", None)
        if not (user and user["owner"]):
            abort(403)
        acc = load_access()
        return render_template("settings.html", access=acc, owner_id=OWNER_ID,
                               characters=characters_getter(), me=user)

    @app.route("/settings/users", methods=["POST"])
    def settings_users():
        user = getattr(g, "dnd_user", None)
        if not (user and user["owner"]):
            return "Forbidden", 403
        data = request.get_json(force=True, silent=True) or {}
        action = data.get("action")
        uid = str(data.get("id", "")).strip()
        acc = load_access()
        if action in ("add", "update"):
            if not _ID_RE.match(uid):
                return "That doesn't look like a Discord user ID (15-22 digits).", 400
            name = str(data.get("name", "")).strip()[:40] or uid
            char = str(data.get("character", "")).strip()[:50]
            if char and not _NAME_RE.match(char):
                return "Character name has odd characters in it.", 400
            if not _NAME_RE.match(name):
                name = uid
            prev = acc["users"].get(uid, {})
            acc["users"][uid] = {"name": name, "character": char, "prefs": prev.get("prefs", {})}
        elif action == "remove":
            if uid == OWNER_ID:
                return "The owner can't be removed.", 400
            acc["users"].pop(uid, None)
        else:
            return "Unknown action", 400
        save_access(acc)
        return jsonify(acc)
