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

# Whether the host currently has recording turned on, per room. Lets a
# participant who joins mid-call immediately see accurate status instead of
# assuming "not recording" until the next toggle.
RECORDING = {}

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
# Waiting room / lobby
# ---------------------------------------------------------------------------

@socketio.on("request_to_join")
def handle_request_to_join(data):
    """First thing a client does on page load. Host is auto-admitted.
    Everyone else waits for the host to approve them, unless no host is
    currently in the room (then we let them straight in rather than stall)."""
    room_code = data.get("room_code")
    if not current_user.is_authenticated:
        return

    meeting = Meeting.query.filter_by(room_code=room_code).first()
    if not meeting:
        return

    sid = request.sid
    is_host = meeting.host_id == current_user.id

    if is_host:
        emit("join_approved", {})
        return

    host_sid = _host_sid_for_room(room_code)
    if not host_sid:
        emit("join_approved", {})
        return

    WAITING.setdefault(room_code, {})[sid] = current_user.name
    emit("join_request", {"sid": sid, "name": current_user.name}, room=host_sid)
    emit("waiting_for_host", {})


@socketio.on("admit_participant")
def handle_admit_participant(data):
    room_code = data.get("room_code")
    target_sid = data.get("sid")
    if not current_user.is_authenticated or not target_sid:
        return

    meeting = Meeting.query.filter_by(room_code=room_code).first()
    if not meeting or meeting.host_id != current_user.id:
        return

    WAITING.get(room_code, {}).pop(target_sid, None)
    emit("join_approved", {}, room=target_sid)


@socketio.on("deny_participant")
def handle_deny_participant(data):
    room_code = data.get("room_code")
    target_sid = data.get("sid")
    if not current_user.is_authenticated or not target_sid:
        return

    meeting = Meeting.query.filter_by(room_code=room_code).first()
    if not meeting or meeting.host_id != current_user.id:
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
    if not meeting:
        return

    is_host = meeting.host_id == current_user.id
    sid = request.sid

    existing_peers = [
        {"sid": s, "name": info["name"], "is_host": info["is_host"]}
        for s, info in CONNECTED.items()
        if info["room_code"] == room_code
    ]

    CONNECTED[sid] = {
        "room_code": room_code,
        "user_id": current_user.id,
        "name": current_user.name,
        "is_host": is_host,
    }

    join_room(room_code)

    if is_host and room_code not in TIMED_ROOMS:
        if not meeting.call_started_at:
            meeting.call_started_at = datetime.utcnow()
            db.session.commit()

        limit_minutes = (
            current_app.config["PREMIUM_CALL_LIMIT_MINUTES"] if meeting.host.is_premium
            else current_app.config["FREE_CALL_LIMIT_MINUTES"]
        )
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


@socketio.on("signal")
def handle_signal(data):
    target = data.get("target")
    if not target:
        return
    emit(
        "signal",
        {"sender": request.sid, "signal": data.get("signal")},
        room=target,
    )


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@socketio.on("chat_message")
def handle_chat(data):
    sid = request.sid
    info = CONNECTED.get(sid)
    if not info:
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
    if not info:
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
    if not info:
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
    if not info:
        return
    sharing = bool(data.get("sharing"))
    emit(
        "screen_share",
        {"sid": sid, "name": info["name"], "sharing": sharing},
        room=info["room_code"],
        include_self=False,
    )


# ---------------------------------------------------------------------------
# Recording status (host decides — see meeting_room.html consent prompt)
# ---------------------------------------------------------------------------

@socketio.on("recording_status")
def handle_recording_status(data):
    room_code = data.get("room_code")
    if not current_user.is_authenticated:
        return
    meeting = Meeting.query.filter_by(room_code=room_code).first()
    if not meeting or meeting.host_id != current_user.id:
        return
    recording = bool(data.get("recording"))
    RECORDING[room_code] = recording
    emit("recording_status", {"recording": recording}, room=room_code)


# ---------------------------------------------------------------------------
# Host controls
# ---------------------------------------------------------------------------

@socketio.on("host_mute_all")
def handle_host_mute_all(data):
    room_code = data.get("room_code")
    if not current_user.is_authenticated:
        return
    meeting = Meeting.query.filter_by(room_code=room_code).first()
    if not meeting or meeting.host_id != current_user.id:
        return
    emit("force_mute", {}, room=room_code, include_self=False)


@socketio.on("host_remove_participant")
def handle_host_remove_participant(data):
    room_code = data.get("room_code")
    target_sid = data.get("sid")
    if not current_user.is_authenticated or not target_sid:
        return
    meeting = Meeting.query.filter_by(room_code=room_code).first()
    if not meeting or meeting.host_id != current_user.id:
        return
    emit("removed_by_host", {}, room=target_sid)


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------

@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid

    # Clear from any waiting-room lists regardless of admission state.
    for pending in WAITING.values():
        pending.pop(sid, None)

    info = CONNECTED.pop(sid, None)
    if not info:
        return

    room_code = info["room_code"]
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
                row.left_at = datetime.utcnow()
                db.session.commit()
