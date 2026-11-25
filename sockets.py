from flask import request, current_app, session
from flask_login import current_user
from flask_socketio import join_room, leave_room, emit
from datetime import datetime

import recording_service
import rtc
from extensions import socketio, db
from models import Meeting, MeetingParticipant, MeetingGuest, SlideDeck

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

# The presentation currently on everyone's screen, per room. Like the
# whiteboard, the host drives it and everybody else just watches it fill their
# display. room_code -> {"deck_id", "title", "pages", "index"}; absent = none.
SLIDES = {}

# The student the teacher has put in the spotlight (and maybe invited to speak).
# room_code -> {"sid", "name", "speak", "accepted"}; absent = nobody.
CALLON = {}
# Where the host was in each deck, so presenting it again resumes there.
# (room_code, deck_id) -> 0-based slide index
SLIDE_POS = {}

# Sockets that belong to people who came in through a lesson's share link (no
# account). sid -> MeetingGuest id.
GUEST_SIDS = {}

# sid -> when a student left the lesson screen (switched tab / left full screen).
AWAY_SINCE = {}

# Rooms that already have a call-length timer scheduled — set stops us
# scheduling a duplicate if the host's client re-emits "join" (e.g. a
# reconnect) partway through a call.
TIMED_ROOMS = set()


def connected_guest_count(room_code):
    return sum(1 for s, info in CONNECTED.items()
               if info["room_code"] == room_code and info.get("guest_id"))


def emit_to_members(room_code, event, data):
    """Send to everyone in the room who has an account. Guests who came in
    through the share link are skipped (used for private things like notes)."""
    for sid, info in list(CONNECTED.items()):
        if info["room_code"] == room_code and not info.get("guest_id"):
            socketio.emit(event, data, room=sid)


def _guest_identity(room_code, meeting):
    """The MeetingGuest behind this socket (registered through /join/<token> in
    this browser), or None. People who already belong through an account use
    the normal path instead."""
    if meeting is None or meeting.status in ("ended", "cancelled"):
        return None
    gid = (session.get("guests") or {}).get(room_code)
    if not gid:
        return None
    if current_user.is_authenticated and meeting.entry_state(current_user) in ("host", "ok"):
        return None
    guest = MeetingGuest.query.get(gid)
    if not guest or guest.meeting_id != meeting.id or guest.removed_at:
        return None
    return guest


def admit_waiting_guests(room_code):
    """Let every waiting link-guest in (used when the host arrives, or switches
    to 'let guests in automatically'). Needs the host to be in the room: guests
    never run ahead of their teacher."""
    if not _host_sid_for_room(room_code):
        return
    for sid in list(WAITING.get(room_code, {})):
        if sid in GUEST_SIDS:
            WAITING[room_code].pop(sid, None)
            ADMITTED.setdefault(room_code, set()).add(sid)
            socketio.emit("join_approved", {}, room=sid)
            # Tell the host the 'waiting' request is resolved.
            host_sid = _host_sid_for_room(room_code)
            if host_sid:
                socketio.emit("join_request_cleared", {"sid": sid}, room=host_sid)


def _host_sid_for_room(room_code):
    return next(
        (s for s, info in CONNECTED.items() if info["room_code"] == room_code and info["is_host"]),
        None,
    )


def _host_info(sid):
    """The CONNECTED entry for this socket if it is the session's host, else None.
    Used by the slide, whiteboard and teaching-tool handlers: only the host may drive them."""
    info = CONNECTED.get(sid)
    return info if info and info.get("is_host") else None


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


def _attendance_row(info):
    """The attendance row for a connected socket: a MeetingGuest for a link
    guest, otherwise the open MeetingParticipant row. Both carry
    away_count / away_seconds / left_at."""
    if info.get("guest_id"):
        return MeetingGuest.query.get(info["guest_id"])
    meeting = Meeting.query.filter_by(room_code=info["room_code"]).first()
    return _open_participant(meeting.id, info["user_id"]) if meeting else None


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
    """First thing a client does on page load. The host is let in straight
    away. Account holders wait for the host to admit them. Guests who came in
    through the share link are admitted automatically (the default) once the
    host is in the room, or wait to be admitted if the host turned that off."""
    room_code = data.get("room_code")
    meeting = Meeting.query.filter_by(room_code=room_code).first()

    guest = None if data.get("observe") else _guest_identity(room_code, meeting)
    if guest:
        sid = request.sid
        GUEST_SIDS[sid] = guest.id
        host_sid = _host_sid_for_room(room_code)
        if meeting.guest_auto_admit and host_sid:
            ADMITTED.setdefault(room_code, set()).add(sid)
            emit("join_approved", {})
            return
        WAITING.setdefault(room_code, {})[sid] = guest.name
        emit("waiting_for_host", {})
        # With auto-admit on and no host yet, they are let in the moment the host arrives.
        if host_sid and not meeting.guest_auto_admit:
            emit("join_request", {"sid": sid, "name": guest.name}, room=host_sid)
        return

    if not current_user.is_authenticated:
        return

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
    meeting = Meeting.query.filter_by(room_code=room_code).first()
    sid = request.sid

    guest = _guest_identity(room_code, meeting) if sid in GUEST_SIDS else None
    if guest is None and not current_user.is_authenticated:
        return

    observing = guest is None and sid in OBSERVERS.get(room_code, set())

    if guest:
        pass  # checked in _guest_identity
    elif observing:
        if not _may_observe(meeting, current_user):
            return
    elif not _may_be_here(meeting, current_user):
        return

    is_host = guest is None and (not observing) and meeting.is_hosted_by(current_user)

    # Must have been through the lobby (admitted) — or be an admin observer.
    if sid not in ADMITTED.get(room_code, set()):
        return

    name = guest.name if guest else current_user.name

    # Observers are invisible: never listed to anyone as a peer.
    existing_peers = [
        {"sid": s, "name": info["name"], "is_host": info["is_host"],
         "sharing": bool(info.get("sharing")), "data_mode": info.get("data_mode", "normal")}
        for s, info in CONNECTED.items()
        if info["room_code"] == room_code and not info.get("observer")
    ]

    CONNECTED[sid] = {
        "room_code": room_code,
        "user_id": None if guest else current_user.id,
        "guest_id": guest.id if guest else None,
        "name": name,
        "is_host": is_host,
        "observer": observing,
        "sharing": False,
        "data_mode": "normal",
    }

    join_room(room_code)

    if guest:
        if not guest.entered_at:
            guest.entered_at = datetime.utcnow()
        guest.left_at = None
        db.session.commit()

    # Whoever arrives (guest, observer, or the host reloading) sees the presentation
    # (or that there isn't one: after a dropped connection, a browser may still be
    # showing slides that have since ended)...
    emit("slides_state", _slides_payload(room_code))
    emit("video_state", _video_payload(room_code))
    # ...and the rest of the room as it is now: the whiteboard, drawing on the slide,
    # a poll in progress, breakout groups, the timer.
    sockets_tools.on_join(sid, room_code)

    if observing:
        # No peer_joined, no participant row — they just receive.
        emit("existing_peers", {"peers": existing_peers, "recording": True})
        return

    if is_host:
        recording_service.host_back(room_code)
        RECORDING[room_code] = True
        emit("recording_status", {"recording": True}, room=room_code)
        # The server decides WHO records: itself (media server) or, failing
        # that, the host's browser. The host's browser waits for this answer.
        socketio.start_background_task(
            recording_service.begin, current_app._get_current_object(), meeting.id, sid
        )

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

    if room_code in CALLON:
        emit("spotlight", _spotlight_payload(room_code))

    # Peer-to-peer only: a busy room can't carry another camera. This person
    # joins audio-only (their browser also stops offering the video options).
    if (not is_host and not rtc.livekit_enabled()
            and len(existing_peers) >= current_app.config["MESH_VIDEO_LIMIT"]):
        emit("mesh_full", {})

    emit(
        "peer_joined",
        {"sid": sid, "name": name, "is_host": is_host},
        room=room_code,
        include_self=False,
    )

    # People who arrived before the host are queued. Link guests walk in if the
    # host allows it; everyone else is shown to the host to admit or deny.
    if is_host:
        if meeting.guest_auto_admit:
            admit_waiting_guests(room_code)
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
# Media server (LiveKit). Lobby, chat, slides etc. stay on Socket.IO; only the
# audio/video moves. A person's LiveKit identity IS their Socket.IO sid, so the
# server can vouch for them here: only a socket that has been admitted to the
# call and is in CONNECTED can get a token, and it can only join its own room.
# ---------------------------------------------------------------------------

@socketio.on("livekit_token")
def handle_livekit_token(data):
    sid = request.sid
    info = CONNECTED.get(sid)
    if not info or sid not in ADMITTED.get(info["room_code"], set()):
        return
    if not rtc.livekit_enabled():
        return
    token = rtc.participant_token(
        info["room_code"], sid, info["name"],
        is_host=info["is_host"], observer=bool(info.get("observer")),
    )
    emit("livekit_token", {"url": current_app.config["LIVEKIT_URL"], "token": token})


# ---------------------------------------------------------------------------
# Low-data mode. A student on a weak connection can drop to smaller video or to
# audio only without leaving the lesson. The choice is only a label for the
# others (the teacher sees who is on a slow link); what is actually sent and
# received is decided in the student's own browser.
# ---------------------------------------------------------------------------

@socketio.on("data_mode")
def handle_data_mode(data):
    sid = request.sid
    info = CONNECTED.get(sid)
    if not info or info.get("observer"):
        return
    mode = (data or {}).get("mode")
    if mode not in ("normal", "low", "audio"):
        mode = "normal"
    info["data_mode"] = mode
    emit("data_mode", {"sid": sid, "mode": mode}, room=info["room_code"], include_self=False)


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
    info["sharing"] = sharing  # remembered so people who join mid-share are told
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
    if away:
        # During a teacher's break (and just after) students may leave the lesson screen.
        import classroom_state as _cs
        tools = _cs.peek(info["room_code"])
        if tools and tools.rest.excuses_absence():
            return
    row = _attendance_row(info)
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
# Presentations. The host uploads a deck (routes/slides.py turns it into one
# picture per slide) and presents it: it covers the whole screen for everyone
# in the session and follows the host's slide changes. Only the host can start,
# stop or move it. Slide numbers are 0-based here; the server validates every
# one, so a student's browser can't drive the presentation.
# ---------------------------------------------------------------------------

def _slides_payload(room_code):
    state = SLIDES.get(room_code)
    if not state:
        return {"active": False}
    return {"active": True, "deck_id": state["deck_id"], "title": state["title"],
            "pages": state["pages"], "index": state["index"], "v": state["v"]}


def _int_in_range(value, lo, hi):
    try:
        return min(hi, max(lo, int(value)))
    except (TypeError, ValueError):
        return None


def stop_slides(room_code):
    """Takes the presentation off everyone's screen."""
    SLIDES.pop(room_code, None)
    socketio.emit("slides_state", {"active": False}, room=room_code)


def clear_slides(room_code):
    """Forget all presentation state for a room (the session ended)."""
    SLIDES.pop(room_code, None)
    for key in [k for k in SLIDE_POS if k[0] == room_code]:
        SLIDE_POS.pop(key, None)


@socketio.on("slides_present")
def handle_slides_present(data):
    info = _host_info(request.sid)
    if not info:
        return
    room_code = info["room_code"]
    meeting = Meeting.query.filter_by(room_code=room_code).first()
    deck_id = _int_in_range((data or {}).get("deck_id"), 1, 2**31 - 1)
    deck = SlideDeck.query.get(deck_id) if deck_id else None
    if (not meeting or meeting.status != "live" or not deck or deck.meeting_id != meeting.id
            or deck.status != "ready" or deck.page_count < 1):
        emit("slides_error", {"message": "That presentation isn't ready to show yet."})
        return

    index = _int_in_range((data or {}).get("index"), 0, deck.page_count - 1)
    if index is None:
        index = _int_in_range(SLIDE_POS.get((room_code, deck.id), 0), 0, deck.page_count - 1) or 0
    SLIDES[room_code] = {"deck_id": deck.id, "title": deck.title, "pages": deck.page_count, "index": index,
                         "v": deck.version}
    SLIDE_POS[(room_code, deck.id)] = index
    # To everyone, the host included: the host's presenter view opens from this
    # same message, so what they see is what the class sees.
    emit("slides_state", _slides_payload(room_code), room=room_code)
    sockets_tools.on_slides_changed(room_code)


@socketio.on("slides_goto")
def handle_slides_goto(data):
    info = _host_info(request.sid)
    state = SLIDES.get(info["room_code"]) if info else None
    if not state:
        return
    index = _int_in_range((data or {}).get("index"), 0, state["pages"] - 1)
    if index is None:
        return
    state["index"] = index
    SLIDE_POS[(info["room_code"], state["deck_id"])] = index
    # The host's own screen already moved; tell everyone else.
    emit("slides_goto", {"index": index}, room=info["room_code"], include_self=False)
    sockets_tools.on_slides_changed(info["room_code"])   # this slide's drawing goes to everyone, the host too


@socketio.on("slides_stop")
def handle_slides_stop(data):
    info = _host_info(request.sid)
    if info and info["room_code"] in SLIDES:
        stop_slides(info["room_code"])


# ---------------------------------------------------------------------------
# YouTube video. The host picks a video and plays it for the whole session. The
# host's player is the master: every play / pause / seek / speed change (and a
# heartbeat every few seconds) goes to the server, which remembers it so people
# who join or reload mid-video start in the right place, and relays it so every
# student's player follows. Only the host can start, drive or stop it.
# ---------------------------------------------------------------------------

import re as _re
import time as _time

VIDEOS = {}   # room_code -> {"id", "title", "t", "playing", "rate", "at"}
VIDEO_RATES = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
_VIDEO_ID_RE = _re.compile(r"^[A-Za-z0-9_-]{11}$")


def _video_payload(room_code):
    v = VIDEOS.get(room_code)
    if not v:
        return {"active": False}
    t = v["t"]
    if v["playing"]:
        t += (_time.time() - v["at"]) * v["rate"]
    return {"active": True, "id": v["id"], "title": v["title"], "t": round(t, 2),
            "playing": v["playing"], "rate": v["rate"]}


def clear_video(room_code):
    VIDEOS.pop(room_code, None)


@socketio.on("video_play")
def handle_video_play(data):
    info = _host_info(request.sid)
    if not info:
        return
    room_code = info["room_code"]
    meeting = Meeting.query.filter_by(room_code=room_code).first()
    video_id = str((data or {}).get("id") or "")
    if not meeting or meeting.status != "live" or not _VIDEO_ID_RE.match(video_id):
        emit("video_error", {"message": "That video link is not valid."})
        return
    title = str((data or {}).get("title") or "")[:120]
    VIDEOS[room_code] = {"id": video_id, "title": title, "t": 0.0, "playing": True,
                         "rate": 1.0, "at": _time.time()}
    emit("video_state", _video_payload(room_code), room=room_code)   # everyone, the host too


@socketio.on("video_sync")
def handle_video_sync(data):
    """A student's browser asks "is a video playing?" It is the safety net for a
    video_state message that never arrived (a dropped or sleeping connection)."""
    info = CONNECTED.get(request.sid)
    if not info:
        return
    emit("video_state", _video_payload(info["room_code"]))


@socketio.on("video_ctrl")
def handle_video_ctrl(data):
    info = _host_info(request.sid)
    v = VIDEOS.get(info["room_code"]) if info else None
    if not v:
        return
    data = data or {}
    try:
        t = min(max(float(data.get("t", v["t"])), 0.0), 86400.0)
    except (TypeError, ValueError):
        return
    try:
        rate = float(data.get("rate", v["rate"]))
    except (TypeError, ValueError):
        rate = v["rate"]
    if rate not in VIDEO_RATES:
        rate = v["rate"]
    v.update({"t": t, "playing": bool(data.get("playing")), "rate": rate, "at": _time.time()})
    emit("video_state", _video_payload(info["room_code"]), room=info["room_code"], include_self=False)


@socketio.on("video_stop")
def handle_video_stop(data):
    info = _host_info(request.sid)
    if info and info["room_code"] in VIDEOS:
        clear_video(info["room_code"])
        emit("video_state", {"active": False}, room=info["room_code"])


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


# ---------------------------------------------------------------------------
# Calling on a student. The teacher can
#   * spotlight a student: everyone sees that student's video large, or
#   * call on them: the same, plus an invitation to speak. A student's microphone
#     is never switched on for them: they get a prompt and press "Unmute and
#     speak" themselves (or "Not now"). The teacher ends the turn, or the student
#     can stop it, and the microphone goes back to how it was.
# Only the host can start or end it (checked here); one student at a time.
# ---------------------------------------------------------------------------

def _spotlight_payload(room_code):
    state = CALLON.get(room_code)
    if not state:
        return {"active": False}
    return {"active": True, "sid": state["sid"], "name": state["name"],
            "speak": state["speak"], "accepted": state["accepted"]}


def _host_of_room(room_code):
    """The host's CONNECTED info if THIS socket is the host of that room."""
    info = CONNECTED.get(request.sid)
    if not info or not info["is_host"] or info["room_code"] != room_code:
        return None
    return info


@socketio.on("host_call_on")
def handle_host_call_on(data):
    room_code = (data or {}).get("room_code")
    host = _host_of_room(room_code)
    target = CONNECTED.get((data or {}).get("sid"))
    if not host or not target or target["room_code"] != room_code:
        return
    if target.get("observer") or target["is_host"]:
        return
    target_sid = data["sid"]
    speak = bool(data.get("speak"))
    previous = CALLON.get(room_code)
    if previous and previous["sid"] != target_sid:
        emit("call_on_end", {}, room=previous["sid"])
    CALLON[room_code] = {"sid": target_sid, "name": target["name"], "speak": speak, "accepted": False}
    emit("spotlight", _spotlight_payload(room_code), room=room_code)
    if speak:
        emit("call_on", {"by": host["name"]}, room=target_sid)
        # Calling on someone answers their raised hand.
        emit("hand_raise", {"sid": target_sid, "name": target["name"], "raised": False}, room=room_code)


@socketio.on("call_on_response")
def handle_call_on_response(data):
    """The called-on student accepts, declines, or (after accepting) stops."""
    sid = request.sid
    info = CONNECTED.get(sid)
    if not info:
        return
    room_code = info["room_code"]
    state = CALLON.get(room_code)
    if not state or state["sid"] != sid:
        return
    if (data or {}).get("accepted") and not state["accepted"]:
        state["accepted"] = True
        emit("spotlight", _spotlight_payload(room_code), room=room_code)
        return
    # Declined, or finished speaking.
    CALLON.pop(room_code, None)
    emit("spotlight", {"active": False}, room=room_code)
    host_sid = _host_sid_for_room(room_code)
    if host_sid:
        emit("call_on_declined", {"name": info["name"], "was_speaking": bool(state["accepted"])}, room=host_sid)


@socketio.on("host_call_on_end")
def handle_host_call_on_end(data):
    room_code = (data or {}).get("room_code")
    if not _host_of_room(room_code):
        return
    state = CALLON.pop(room_code, None)
    if state:
        emit("call_on_end", {}, room=state["sid"])
    emit("spotlight", {"active": False}, room=room_code)


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
    rtc.remove_participant(room_code, target_sid)
    if CALLON.get(room_code, {}).get("sid") == target_sid:
        CALLON.pop(room_code, None)
        emit("spotlight", {"active": False}, room=room_code)
    gid = GUEST_SIDS.get(target_sid)
    if gid:
        # A removed link guest can't simply click the link again.
        removed = MeetingGuest.query.get(gid)
        if removed and removed.meeting_id == meeting.id:
            removed.removed_at = datetime.utcnow()
            db.session.commit()
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

    GUEST_SIDS.pop(sid, None)

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
    sockets_tools.on_leave(sid, room_code)

    if CALLON.get(room_code, {}).get("sid") == sid:
        CALLON.pop(room_code, None)
        emit("spotlight", {"active": False}, room=room_code)

    if info["is_host"]:
        RECORDING.pop(room_code, None)
        # If the host doesn't come back, end the session for them and save the
        # recording (otherwise it would sit "live" and never be saved).
        meeting_for_save = Meeting.query.filter_by(room_code=room_code).first()
        if meeting_for_save and meeting_for_save.status == "live":
            recording_service.host_dropped(current_app._get_current_object(), meeting_for_save.id, room_code)

    if info.get("guest_id"):
        row = MeetingGuest.query.get(info["guest_id"])
        if row:
            _close_away(sid, row)
            row.left_at = datetime.utcnow()
            db.session.commit()
        return

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


# The teaching tools (whiteboard, drawing on slides, polls, breakout rooms, timer).
# Imported last because it uses names defined above.
import sockets_tools  # noqa: E402,F401
