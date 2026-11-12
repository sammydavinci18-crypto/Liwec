from flask import request, current_app
from flask_login import current_user
from flask_socketio import join_room, leave_room, emit
from datetime import datetime

from extensions import socketio, db
from models import Meeting, MeetingParticipant

# Tracks which socket id belongs to which (room_code, user) so we can
# tell everyone else in the room when someone disconnects.
CONNECTED = {}  # sid -> {"room_code": str, "user_id": int, "name": str, "is_host": bool}

# Participants who have requested to join but haven't been admitted yet.
# room_code -> { sid: name }
WAITING = {}

# Sockets the admin host has actually let in (the host's own socket is added
# automatically). A socket that is not in here can't join the call or relay
# signals — so skipping the lobby by emitting "join" directly does nothing.
# room_code -> set(sid)
ADMITTED = {}

# Every session is recorded, automatically, from the moment the host is in the
# room. The SERVER decides this (clients can't switch it off or claim it is
# off): set when the host joins, cleared when they disconnect.
RECORDING = {}

# Admins quietly watching a live session. These sockets are admitted without
# the lobby, receive everyone's media, but are never announced to anyone: not
# in existing_peers, no peer_joined / peer_left, no attendance row.
# room_code -> set(sid)
OBSERVERS = {}

# Whiteboard state per room, so someone who joins (or reloads) while the board
# is open sees exactly what is on it. room_code -> {"open": bool, "ops": [op]}
WHITEBOARD = {}
MAX_WHITEBOARD_OPS = 20000

# sid -> when a student left the lesson screen (switched tab / left full screen).
AWAY_SINCE = {}

# Rooms that already have a call-length timer scheduled — set stops us
# scheduling a duplicate if the host's client re-emits "join" (e.g. a
# reconnect) partway through a call.
TIMED_ROOMS = set()


def _host_sid_for_room(room_code):
    return next(
        (s for s, info in CONNECTED.items() if info["room_code"] == room_code and info["is_host"]),
        None,
    )


def _enforce_call_time_limit(app, room_code, meeting_id, limit_minutes):
    """Runs in a background thread (socketio.start_background_task), one
    per call. Warns everyone 5 minutes before the limit, then force-ends
    the call. This is a *soft* enforcement — since calls are peer-to-peer
    (no media server in the middle), the server can tell every client to
    hang up and can stop treating the room as live, but it can't literally
    cut the media stream the way a server-routed call (like Zoom) can. For
    this product's purposes (a feature, not an adversarial security
    boundary) that's an acceptable tradeoff — worth knowing about, though.
    """
    warn_at = max(limit_minutes - 5, 0) * 60
    remaining_after_warn = min(limit_minutes, 5) * 60

    socketio.sleep(warn_at)
    with app.app_context():
        meeting = Meeting.query.get(meeting_id)
        if not meeting or meeting.status != "live":
            TIMED_ROOMS.discard(room_code)
            return
        socketio.emit("time_limit_warning", {"minutes_left": min(limit_minutes, 5)}, room=room_code)

    socketio.sleep(remaining_after_warn)
    with app.app_context():
        meeting = Meeting.query.get(meeting_id)
        if meeting and meeting.status == "live":
            socketio.emit("time_limit_reached", {}, room=room_code)
    TIMED_ROOMS.discard(room_code)


# ---------------------------------------------------------------------------
# Waiting room / lobby — the admin decides who comes in. Always.
# ---------------------------------------------------------------------------

def _may_observe(meeting, user):
    """Any admin may watch a live session they don't host. Nobody else can."""
    return (
        meeting is not None and meeting.status == "live"
        and user.is_authenticated and user.is_admin
        and not meeting.is_hosted_by(user)
    )


def _open_participant(meeting_id, user_id):
    return (
        MeetingParticipant.query.filter_by(meeting_id=meeting_id, user_id=user_id, left_at=None)
        .order_by(MeetingParticipant.joined_at.desc()).first()
    )


def _close_away(sid, row):
    """A student is back (or gone): add the time they were away to their row."""
    since = AWAY_SINCE.pop(sid, None)
    if since and row:
        row.away_seconds = (row.away_seconds or 0) + max(0, int((datetime.utcnow() - since).total_seconds()))


def _may_be_here(meeting, user):
    """Only the admin host, or a guest on the admin's invite list."""
    return meeting is not None and meeting.status not in ("ended", "cancelled") \
        and meeting.entry_state(user) in ("host", "ok")


@socketio.on("request_to_join")
def handle_request_to_join(data):
    """First thing a client does on page load. The admin host is let in
    straight away. Every guest waits for the admin to admit them — including
    when the admin hasn't arrived yet (guests queue up and the admin sees
    them the moment they open the room). Nobody is ever auto-admitted."""
    room_code = data.get("room_code")
    if not current_user.is_authenticated:
        return

    meeting = Meeting.query.filter_by(room_code=room_code).first()

    if data.get("observe"):
        # Silent oversight: straight in, no lobby, nobody is told.
        if not _may_observe(meeting, current_user):
            emit("join_denied", {})
            return
        ADMITTED.setdefault(room_code, set()).add(request.sid)
        OBSERVERS.setdefault(room_code, set()).add(request.sid)
        emit("join_approved", {})
        return

    if not _may_be_here(meeting, current_user):
        emit("join_denied", {})
        return

    sid = request.sid

    if meeting.is_hosted_by(current_user):
        ADMITTED.setdefault(room_code, set()).add(sid)
        emit("join_approved", {})
        return

    WAITING.setdefault(room_code, {})[sid] = current_user.name
    emit("waiting_for_host", {})
    host_sid = _host_sid_for_room(room_code)
    if host_sid:
        emit("join_request", {"sid": sid, "name": current_user.name}, room=host_sid)


@socketio.on("admit_participant")
def handle_admit_participant(data):
    room_code = data.get("room_code")
    target_sid = data.get("sid")
    if not current_user.is_authenticated or not target_sid:
        return

    meeting = Meeting.query.filter_by(room_code=room_code).first()
    if not meeting or not meeting.is_hosted_by(current_user):
        return

    # Can only admit someone who is genuinely waiting in this room.
    if target_sid not in WAITING.get(room_code, {}):
        return
    WAITING[room_code].pop(target_sid, None)
    ADMITTED.setdefault(room_code, set()).add(target_sid)
    emit("join_approved", {}, room=target_sid)


@socketio.on("deny_participant")
def handle_deny_participant(data):
    room_code = data.get("room_code")
    target_sid = data.get("sid")
    if not current_user.is_authenticated or not target_sid:
        return

    meeting = Meeting.query.filter_by(room_code=room_code).first()
    if not meeting or not meeting.is_hosted_by(current_user):
        return

    WAITING.get(room_code, {}).pop(target_sid, None)
    emit("join_denied", {}, room=target_sid)


# ---------------------------------------------------------------------------
# Mesh call join / signaling (unchanged flow, just gated behind admission now)
# ---------------------------------------------------------------------------

@socketio.on("join")
def handle_join(data):
    room_code = data.get("room_code")
    if not current_user.is_authenticated:
        return

    meeting = Meeting.query.filter_by(room_code=room_code).first()
    sid = request.sid
    observing = sid in OBSERVERS.get(room_code, set())

    if observing:
        if not _may_observe(meeting, current_user):
            return
    elif not _may_be_here(meeting, current_user):
        return

    is_host = (not observing) and meeting.is_hosted_by(current_user)

    # Must have been through the lobby (admitted by the host) — or be an admin observer.
    if sid not in ADMITTED.get(room_code, set()):
        return

    # Observers are invisible: never listed to anyone as a peer.
    existing_peers = [
        {"sid": s, "name": info["name"], "is_host": info["is_host"]}
        for s, info in CONNECTED.items()
        if info["room_code"] == room_code and not info.get("observer")
    ]

    CONNECTED[sid] = {
        "room_code": room_code,
        "user_id": current_user.id,
        "name": current_user.name,
        "is_host": is_host,
        "observer": observing,
    }

    join_room(room_code)

    # Whoever arrives (guest, observer, or the host reloading) sees the current board.
    board = WHITEBOARD.get(room_code)
    if board and (board["open"] or board["ops"]):
        emit("wb_state", {"open": board["open"], "ops": board["ops"]})

    if observing:
        # No peer_joined, no participant row — they just receive.
        emit("existing_peers", {"peers": existing_peers, "recording": True})
        return

    if is_host:
        RECORDING[room_code] = True
        emit("recording_status", {"recording": True}, room=room_code)

    if is_host and room_code not in TIMED_ROOMS:
        if not meeting.call_started_at:
            meeting.call_started_at = datetime.utcnow()
            db.session.commit()

        limit_minutes = current_app.config["CALL_LIMIT_MINUTES"]  # 0 = no limit
        if limit_minutes and limit_minutes > 0:
            TIMED_ROOMS.add(room_code)
            app_obj = current_app._get_current_object()
            socketio.start_background_task(
                _enforce_call_time_limit, app_obj, room_code, meeting.id, limit_minutes
            )

    emit("existing_peers", {"peers": existing_peers, "recording": RECORDING.get(room_code, False)})
    emit(
        "peer_joined",
        {"sid": sid, "name": current_user.name, "is_host": is_host},
        room=room_code,
        include_self=False,
    )

    # Guests who arrived before the admin are queued — show them to the admin now.
    if is_host:
        for waiting_sid, waiting_name in WAITING.get(room_code, {}).items():
            emit("join_request", {"sid": waiting_sid, "name": waiting_name})


@socketio.on("signal")
def handle_signal(data):
    target = data.get("target")
    if not target:
        return
    # Signals only pass between two people who are both inside the same call.
    sender_info = CONNECTED.get(request.sid)
    target_info = CONNECTED.get(target)
    if not sender_info or not target_info or sender_info["room_code"] != target_info["room_code"]:
        return
    emit(
        "signal",
        # Lets receiving browsers keep an observer's connection out of the UI.
        {"sender": request.sid, "signal": data.get("signal"), "observer": bool(sender_info.get("observer"))},
        room=target,
    )


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@socketio.on("chat_message")
def handle_chat(data):
    sid = request.sid
    info = CONNECTED.get(sid)
    if not info or info.get("observer"):
        return
    emit(
        "chat_message",
        {"name": info["name"], "message": data.get("message", "")[:2000]},
        room=info["room_code"],
    )


# ---------------------------------------------------------------------------
# Reactions / hand raise
# ---------------------------------------------------------------------------

@socketio.on("reaction")
def handle_reaction(data):
    sid = request.sid
    info = CONNECTED.get(sid)
    if not info or info.get("observer"):
        return
    emoji = (data.get("emoji") or "")[:8]
    if not emoji:
        return
    emit(
        "reaction",
        {"sid": sid, "name": info["name"], "emoji": emoji},
        room=info["room_code"],
    )


@socketio.on("hand_raise")
def handle_hand_raise(data):
    sid = request.sid
    info = CONNECTED.get(sid)
    if not info or info.get("observer"):
        return
    raised = bool(data.get("raised"))
    emit(
        "hand_raise",
        {"sid": sid, "name": info["name"], "raised": raised},
        room=info["room_code"],
    )


# ---------------------------------------------------------------------------
# Screen share presence (actual media swap happens over the existing
# WebRTC peer connections — this just lets the UI show a "presenting" badge)
# ---------------------------------------------------------------------------

@socketio.on("screen_share")
def handle_screen_share(data):
    sid = request.sid
    info = CONNECTED.get(sid)
    if not info or info.get("observer"):
        return
    sharing = bool(data.get("sharing"))
    emit(
        "screen_share",
        {"sid": sid, "name": info["name"], "sharing": sharing},
        room=info["room_code"],
        include_self=False,
    )


# ---------------------------------------------------------------------------
# Recording is automatic and compulsory. There is deliberately no handler that
# lets a client turn it off or report it as off: the server marks a room as
# recording when the host joins (see handle_join), and only ending the session
# stops it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Compulsory attendance — students in teacher-led lessons report when they
# leave / return to the lesson screen. It's logged for the record and the
# teacher is told. (A browser can't be stopped from losing focus; what the
# platform can do is block the screen, log it, and tell the teacher.)
# ---------------------------------------------------------------------------

@socketio.on("attendance_away")
def handle_attendance_away(data):
    sid = request.sid
    info = CONNECTED.get(sid)
    if not info or info["is_host"] or info.get("observer"):
        return
    meeting = Meeting.query.filter_by(room_code=info["room_code"]).first()
    if not meeting or not meeting.attendance_required():
        return

    away = bool(data.get("away"))
    row = _open_participant(meeting.id, info["user_id"])
    if away and sid not in AWAY_SINCE:
        AWAY_SINCE[sid] = datetime.utcnow()
        if row:
            row.away_count = (row.away_count or 0) + 1
    elif not away and sid in AWAY_SINCE:
        _close_away(sid, row)
    else:
        return
    db.session.commit()

    host_sid = _host_sid_for_room(info["room_code"])
    if host_sid:
        emit("student_away", {"name": info["name"], "away": away}, room=host_sid)


# ---------------------------------------------------------------------------
# Whiteboard. Only the host draws or types; everyone else (guests and any
# observing admin) just watches it appear full screen. Every operation is
# validated here, kept for late joiners, and relayed. Coordinates are 0..1
# fractions of the board so every screen size shows the same picture.
# ---------------------------------------------------------------------------

def _num(v, lo, hi, default):
    try:
        return min(hi, max(lo, float(v)))
    except (TypeError, ValueError):
        return default


def _colour(v):
    v = v if isinstance(v, str) else ""
    return v if len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:]) else "#111111"


def _points(raw):
    out = []
    for pt in (raw if isinstance(raw, list) else [])[:200]:
        if isinstance(pt, (list, tuple)) and len(pt) == 2:
            out.append([round(_num(pt[0], 0, 1, 0), 4), round(_num(pt[1], 0, 1, 0), 4)])
    return out


def _clean_wb_op(op):
    """Returns a safe copy of a whiteboard operation, or None if it is malformed."""
    if not isinstance(op, dict):
        return None
    kind = op.get("t")
    if kind == "line":
        pts = _points(op.get("p"))
        if not pts:
            return None
        return {"t": "line", "id": str(op.get("id", ""))[:24], "c": _colour(op.get("c")),
                "w": _num(op.get("w"), 0.001, 0.05, 0.006), "p": pts}
    if kind == "more":
        pts = _points(op.get("p"))
        return {"t": "more", "id": str(op.get("id", ""))[:24], "p": pts} if pts else None
    if kind == "text":
        text = str(op.get("s", ""))[:500]
        if not text.strip():
            return None
        return {"t": "text", "s": text, "c": _colour(op.get("c")),
                "x": _num(op.get("x"), 0, 1, 0), "y": _num(op.get("y"), 0, 1, 0),
                "size": _num(op.get("size"), 0.01, 0.2, 0.06)}
    if kind in ("undo", "clear"):
        return {"t": kind}
    return None


def _host_info(sid):
    info = CONNECTED.get(sid)
    return info if info and info["is_host"] and not info.get("observer") else None


def _board(room_code):
    return WHITEBOARD.setdefault(room_code, {"open": False, "ops": []})


@socketio.on("wb_open")
def handle_wb_open(data):
    info = _host_info(request.sid)
    if not info:
        return
    _board(info["room_code"])["open"] = True
    emit("wb_open", {}, room=info["room_code"], include_self=False)


@socketio.on("wb_close")
def handle_wb_close(data):
    info = _host_info(request.sid)
    if not info:
        return
    _board(info["room_code"])["open"] = False
    emit("wb_close", {}, room=info["room_code"], include_self=False)


@socketio.on("wb_op")
def handle_wb_op(data):
    info = _host_info(request.sid)
    if not info:
        return
    op = _clean_wb_op((data or {}).get("op"))
    if not op:
        return
    board = _board(info["room_code"])
    if op["t"] == "clear":
        board["ops"] = []
    elif op["t"] == "undo":
        # Remove the last drawn item together with any "more" points that extended it.
        stored = board["ops"]
        while stored and stored[-1]["t"] == "more":
            stored.pop()
        if stored:
            stored.pop()
    elif len(board["ops"]) < MAX_WHITEBOARD_OPS:
        board["ops"].append(op)
    emit("wb_op", {"op": op}, room=info["room_code"], include_self=False)


# ---------------------------------------------------------------------------
# Host controls
# ---------------------------------------------------------------------------

@socketio.on("host_mute_all")
def handle_host_mute_all(data):
    room_code = data.get("room_code")
    if not current_user.is_authenticated:
        return
    meeting = Meeting.query.filter_by(room_code=room_code).first()
    if not meeting or not meeting.is_hosted_by(current_user):
        return
    emit("force_mute", {}, room=room_code, include_self=False)


@socketio.on("host_remove_participant")
def handle_host_remove_participant(data):
    room_code = data.get("room_code")
    target_sid = data.get("sid")
    if not current_user.is_authenticated or not target_sid:
        return
    meeting = Meeting.query.filter_by(room_code=room_code).first()
    if not meeting or not meeting.is_hosted_by(current_user):
        return
    # Removed guests lose their admission and must be let in again by the admin.
    ADMITTED.get(room_code, set()).discard(target_sid)
    emit("removed_by_host", {}, room=target_sid)


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------

@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid

    # Clear from any waiting-room / admitted lists regardless of state.
    for pending in WAITING.values():
        pending.pop(sid, None)
    for admitted in ADMITTED.values():
        admitted.discard(sid)

    info = CONNECTED.pop(sid, None)
    if not info:
        return

    room_code = info["room_code"]

    if info.get("observer"):
        # Leave no trace for participants: no peer_left, no attendance row. The
        # only signal is an inert one so browsers can close the silent connection.
        OBSERVERS.get(room_code, set()).discard(sid)
        emit("observer_left", {"sid": sid}, room=room_code)
        return

    emit("peer_left", {"sid": sid}, room=room_code)

    if info["is_host"]:
        RECORDING.pop(room_code, None)

    if not info["is_host"]:
        meeting = Meeting.query.filter_by(room_code=room_code).first()
        if meeting:
            row = (
                MeetingParticipant.query.filter_by(
                    meeting_id=meeting.id, user_id=info["user_id"], left_at=None
                )
                .order_by(MeetingParticipant.joined_at.desc())
                .first()
            )
            if row:
                _close_away(sid, row)
                row.left_at = datetime.utcnow()
                db.session.commit()
