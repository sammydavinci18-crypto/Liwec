"""Real-time media plumbing, kept apart from the Socket.IO event handlers.

  * ice_servers()      what each browser is told about STUN / TURN (item: students
                       on mobile data or strict school Wi-Fi need a TURN relay).
  * LiveKit helpers    access tokens, room clean-up, removing a participant.
  * Egress helpers     server-side recording of a lesson (so a teacher's slow
                       laptop or crashed browser can't ruin it).

No third-party SDKs: LiveKit's server API is plain JSON over HTTPS ("Twirp") and
its access tokens are ordinary HS256 JWTs, so `requests` + `hmac` are enough and
nothing new has to be installed.
"""
import base64
import hashlib
import hmac
import json
import threading
import time

import requests
from flask import current_app


# ---------------------------------------------------------------------------
# STUN / TURN for the peer-to-peer mesh (and a safety net when LiveKit is off)
# ---------------------------------------------------------------------------

_CF_CACHE = {"expires": 0, "servers": None}
_CF_LOCK = threading.Lock()


def _coturn_rest_credentials(secret, ttl, label):
    """coturn "use-auth-secret" scheme: the username carries its own expiry and
    the password is an HMAC of it, so the app never stores per-user accounts and
    anything a browser is given stops working after `ttl` seconds."""
    username = f"{int(time.time()) + ttl}:{label}"
    digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode()


def _cloudflare_turn(cfg):
    """Short-lived Cloudflare Realtime TURN credentials, cached so one fetch
    serves everybody joining in the next while."""
    now = time.time()
    with _CF_LOCK:
        if _CF_CACHE["servers"] and _CF_CACHE["expires"] > now + 300:
            return _CF_CACHE["servers"]
    ttl = max(900, int(cfg.get("TURN_TTL_SECONDS") or 21600))
    try:
        r = requests.post(
            f"https://rtc.live.cloudflare.com/v1/turn/keys/{cfg['CLOUDFLARE_TURN_KEY_ID']}/credentials/generate",
            headers={"Authorization": f"Bearer {cfg['CLOUDFLARE_TURN_API_TOKEN']}"},
            json={"ttl": ttl},
            timeout=6,
        )
        r.raise_for_status()
        data = r.json().get("iceServers")
        servers = data if isinstance(data, list) else [data]
        servers = [s for s in servers if s]
    except Exception as exc:  # noqa: BLE001 - never block a lesson on this
        current_app.logger.warning("Cloudflare TURN credentials unavailable: %s", exc)
        return []
    with _CF_LOCK:
        _CF_CACHE.update(expires=now + ttl, servers=servers)
    return servers


def turn_configured(cfg=None):
    cfg = cfg or current_app.config
    if cfg.get("TURN_URLS") and cfg.get("TURN_SECRET"):
        return True
    if cfg.get("CLOUDFLARE_TURN_KEY_ID") and cfg.get("CLOUDFLARE_TURN_API_TOKEN"):
        return True
    return any(u.startswith(("turn:", "turns:")) for u in cfg.get("ICE_SERVERS", []))


def ice_servers(label="user"):
    """The `iceServers` list for one browser."""
    cfg = current_app.config
    servers = []

    for url in cfg["ICE_SERVERS"]:
        entry = {"urls": url}
        if url.startswith(("turn:", "turns:")) and cfg.get("TURN_USERNAME"):
            entry["username"] = cfg["TURN_USERNAME"]
            entry["credential"] = cfg["TURN_CREDENTIAL"]
        servers.append(entry)

    if cfg.get("TURN_URLS") and cfg.get("TURN_SECRET"):
        user, cred = _coturn_rest_credentials(cfg["TURN_SECRET"], int(cfg["TURN_TTL_SECONDS"]), label)
        servers.append({"urls": list(cfg["TURN_URLS"]), "username": user, "credential": cred})

    if cfg.get("CLOUDFLARE_TURN_KEY_ID") and cfg.get("CLOUDFLARE_TURN_API_TOKEN"):
        servers.extend(_cloudflare_turn(cfg))

    return servers


# ---------------------------------------------------------------------------
# LiveKit
# ---------------------------------------------------------------------------

def livekit_enabled(cfg=None):
    cfg = cfg or current_app.config
    return bool(cfg.get("LIVEKIT_URL") and cfg.get("LIVEKIT_API_KEY") and cfg.get("LIVEKIT_API_SECRET"))


def _http_base(cfg):
    if cfg.get("LIVEKIT_HTTP_URL"):
        return cfg["LIVEKIT_HTTP_URL"]
    url = cfg["LIVEKIT_URL"]
    if url.startswith("wss://"):
        return "https://" + url[6:]
    if url.startswith("ws://"):
        return "http://" + url[5:]
    return url


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt(payload, secret):
    head = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64url(sig)}"


def _livekit_token(identity, video, ttl, name="", metadata=""):
    cfg = current_app.config
    now = int(time.time())
    payload = {
        "iss": cfg["LIVEKIT_API_KEY"],
        "sub": identity,
        "nbf": now - 10,
        "exp": now + ttl,
        "video": video,
    }
    if name:
        payload["name"] = name
    if metadata:
        payload["metadata"] = metadata
    return _jwt(payload, cfg["LIVEKIT_API_SECRET"])


def participant_token(room, identity, name, is_host=False, observer=False):
    """A join token for one browser.

    `identity` is the person's Socket.IO sid, so a LiveKit participant and a
    person in the participant list are the same thing everywhere in the room
    code (tiles, hands up, screen-share badges, removal).

    An observing admin gets a *hidden*, receive-only token: LiveKit itself never
    tells anybody they are there."""
    video = {
        "room": room,
        "roomJoin": True,
        "canSubscribe": True,
        "canPublish": not observer,
        "canPublishData": False,
        "hidden": bool(observer),
    }
    meta = json.dumps({"host": bool(is_host)})
    return _livekit_token(identity, video, ttl=6 * 3600, name=name, metadata=meta)


def _call(service, method, body, grants, timeout=10):
    cfg = current_app.config
    token = _livekit_token("server", grants, ttl=60)
    r = requests.post(
        f"{_http_base(cfg)}/twirp/livekit.{service}/{method}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=timeout,
    )
    if r.status_code != 200:
        raise RuntimeError(f"LiveKit {service}.{method} failed ({r.status_code}): {r.text[:300]}")
    return r.json() if r.content else {}


def remove_participant(room, identity):
    """Cut a removed student's media at the server (not just in their browser)."""
    if not livekit_enabled():
        return
    try:
        _call("RoomService", "RemoveParticipant", {"room": room, "identity": identity},
              {"roomAdmin": True, "room": room})
    except Exception as exc:  # noqa: BLE001
        current_app.logger.info("remove_participant: %s", exc)


def delete_room(room):
    """Ending a lesson disconnects everybody from the media server too."""
    if not livekit_enabled():
        return
    try:
        _call("RoomService", "DeleteRoom", {"room": room}, {"roomCreate": True})
    except Exception as exc:  # noqa: BLE001
        current_app.logger.info("delete_room: %s", exc)


# ---------------------------------------------------------------------------
# Server-side recording (LiveKit Egress)
# ---------------------------------------------------------------------------

def egress_enabled(cfg=None):
    cfg = cfg or current_app.config
    return bool(
        livekit_enabled(cfg)
        and cfg.get("PUBLIC_BASE_URL")
        and cfg.get("EGRESS_S3_ENDPOINT")
        and cfg.get("EGRESS_S3_ACCESS_KEY")
        and cfg.get("EGRESS_S3_SECRET")
        and cfg.get("SUPABASE_URL")
        and cfg.get("SUPABASE_SERVICE_KEY")
    )


def egress_view_token(room_code, ttl=12 * 3600):
    """Proves to /egress/<room>/<token>/... that the request comes from the
    recorder we started (it opens our page in a headless browser with no login)."""
    exp = int(time.time()) + ttl
    msg = f"egress:{room_code}:{exp}"
    sig = hmac.new(current_app.config["SECRET_KEY"].encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}-{sig}"


def egress_view_token_ok(room_code, token):
    try:
        exp, sig = token.split("-", 1)
        exp_i = int(exp)
    except (ValueError, AttributeError):
        return False
    if exp_i < time.time():
        return False
    msg = f"egress:{room_code}:{exp_i}"
    good = hmac.new(current_app.config["SECRET_KEY"].encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(good, sig)


def _egress_grants(room):
    return {"roomRecord": True, "room": room}


def start_egress(meeting, filename):
    """Starts recording the room. Returns the egress id, or raises."""
    cfg = current_app.config
    room = meeting.room_code
    url = f"{cfg['PUBLIC_BASE_URL']}/egress/{room}/{egress_view_token(room)}/view"
    body = {
        "room_name": room,
        "layout": "speaker",
        "custom_base_url": url,
        "audio_only": False,
        "file_outputs": [{
            "filepath": filename,
            "file_type": "MP4",
            "s3": {
                "access_key": cfg["EGRESS_S3_ACCESS_KEY"],
                "secret": cfg["EGRESS_S3_SECRET"],
                "region": cfg["EGRESS_S3_REGION"],
                "endpoint": cfg["EGRESS_S3_ENDPOINT"],
                "bucket": cfg["RECORDINGS_BUCKET"],
                "force_path_style": True,
            },
        }],
        "preset": "H264_720P_30",
    }
    info = _call("Egress", "StartRoomCompositeEgress", body, _egress_grants(room), timeout=20)
    egress_id = info.get("egress_id") or info.get("egressId")
    if not egress_id:
        raise RuntimeError(f"Egress did not return an id: {str(info)[:200]}")
    return egress_id


def stop_egress(egress_id, room):
    try:
        _call("Egress", "StopEgress", {"egress_id": egress_id}, _egress_grants(room))
    except Exception as exc:  # noqa: BLE001 - it may already have ended
        current_app.logger.info("stop_egress: %s", exc)


def _status_name(raw):
    names = ["EGRESS_STARTING", "EGRESS_ACTIVE", "EGRESS_ENDING", "EGRESS_COMPLETE",
             "EGRESS_FAILED", "EGRESS_ABORTED", "EGRESS_LIMIT_REACHED"]
    if isinstance(raw, int) and 0 <= raw < len(names):
        return names[raw]
    return str(raw or "")


def egress_status(egress_id, room):
    """'active' | 'complete' | 'failed' | 'unknown' (not listed any more)."""
    try:
        info = _call("Egress", "ListEgress", {"egress_id": egress_id}, _egress_grants(room))
    except Exception as exc:  # noqa: BLE001
        current_app.logger.info("egress_status: %s", exc)
        return "unknown"
    items = info.get("items") or []
    if not items:
        return "unknown"
    status = _status_name(items[0].get("status"))
    if status in ("EGRESS_STARTING", "EGRESS_ACTIVE", "EGRESS_ENDING"):
        return "active"
    if status == "EGRESS_COMPLETE":
        return "complete"
    return "failed"
