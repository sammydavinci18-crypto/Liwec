"""
Socket.IO handlers for the classroom teaching tools. The rules live in
classroom_state.py; this file checks WHO is asking and relays the result.

  Whiteboard   wb_*       teacher draws; one student may be allowed to write
  Slide ink    ink_*, laser   the teacher draws / points on top of slides
  Polls        poll_*     "raise A, B, C or D" with a live count
  Breakouts    breakout_* pairs / groups, then everyone back
  Timer        timer_*    a countdown with an optional task and focus bar

Only the host can start, change or end any of these. A student's browser can
only answer a poll, ask for help in a breakout room, or (when the teacher has
allowed it) write on the whiteboard. Breakout audio separation is applied in
each browser, like the rest of the room's media rules (see README).
"""
from flask_socketio import emit

import classroom_state as cs
from extensions import socketio
from sockets import CONNECTED, SLIDES, _host_info, _host_sid_for_room, _int_in_range


# ---------------------------------------------------------------------------
# Who is in the room
# ---------------------------------------------------------------------------

def _students(room_code):
    """{sid: name} of the people who can be put in groups or asked a question:
    everyone connected except the host and silent observers."""
    return {sid: info["name"] for sid, info in CONNECTED.items()
            if info["room_code"] == room_code and not info["is_host"] and not info.get("observer")}


def _names(room_code):
    return {sid: info["name"] for sid, info in CONNECTED.items() if info["room_code"] == room_code}


def _room_of(sid):
    info = CONNECTED.get(sid)
    return info["room_code"] if info else None


def _to_host(room_code, event, data):
    host = _host_sid_for_room(room_code)
    if host:
        socketio.emit(event, data, room=host)


# ---------------------------------------------------------------------------
# Whiteboard
# ---------------------------------------------------------------------------

def wb_payload(room_code):
    return cs.tools(room_code).board.payload()


def _wb_send_state(room_code):
    socketio.emit("wb_state", wb_payload(room_code), room=room_code)


@socketio.on("wb_open")
def handle_wb_open(data):
    info = _host_info(_sid())
    if not info:
        return
    cs.tools(info["room_code"]).board.set_open(True)
    emit("wb_open", {}, room=info["room_code"], include_self=False)


@socketio.on("wb_close")
def handle_wb_close(data):
    info = _host_info(_sid())
    if not info:
        return
    board = cs.tools(info["room_code"]).board
    had_writer = board.writer is not None
    board.set_open(False)
    emit("wb_close", {}, room=info["room_code"], include_self=False)
    if had_writer:
        socketio.emit("wb_writer", {"writer": None}, room=info["room_code"])


@socketio.on("wb_op")
def handle_wb_op(data):
    sid = _sid()
    info = CONNECTED.get(sid)
    if not info or info.get("observer"):
        return
    board = cs.tools(info["room_code"]).board
    if info["is_host"]:
        allow = cs.ALL_OPS
    elif board.open and board.writer and board.writer["sid"] == sid:
        allow = cs.STUDENT_OPS              # the student the teacher let write: strokes, shapes and text only
    else:
        return
    op = cs.clean_ink_op((data or {}).get("op"), allow)
    if op and board.apply(op):
        emit("wb_op", {"op": op}, room=info["room_code"], include_self=False)


@socketio.on("wb_done")
def handle_wb_done(data):
    """The student the teacher let write says they are finished."""
    sid = _sid()
    info = CONNECTED.get(sid)
    if not info:
        return
    board = cs.tools(info["room_code"]).board
    if board.writer and board.writer["sid"] == sid:
        board.set_writer(None, None)
        socketio.emit("wb_writer", {"writer": None}, room=info["room_code"])


@socketio.on("wb_page")
def handle_wb_page(data):
    info = _host_info(_sid())
    if not info:
        return
    board = cs.tools(info["room_code"]).board
    data = data or {}
    changed = board.add_page() if data.get("add") else board.go_to(data.get("page"))
    if changed:
        _wb_send_state(info["room_code"])


@socketio.on("wb_bg")
def handle_wb_bg(data):
    info = _host_info(_sid())
    if info and cs.tools(info["room_code"]).board.set_bg((data or {}).get("bg")):
        _wb_send_state(info["room_code"])


@socketio.on("wb_ruler")
def handle_wb_ruler(data):
    info = _host_info(_sid())
    if info and cs.tools(info["room_code"]).board.set_ruler(data):
        emit("wb_ruler", {"ruler": cs.tools(info["room_code"]).board.ruler}, room=info["room_code"], include_self=False)


@socketio.on("wb_allow")
def handle_wb_allow(data):
    """The teacher lets one student write on the board (or takes it back: sid null)."""
    info = _host_info(_sid())
    if not info:
        return
    room_code = info["room_code"]
    board = cs.tools(room_code).board
    target = (data or {}).get("sid")
    students = _students(room_code)
    if target and target not in students:
        return
    board.set_writer(target, students.get(target))
    socketio.emit("wb_writer", {"writer": board.writer}, room=room_code)


# ---------------------------------------------------------------------------
# Drawing on slides
# ---------------------------------------------------------------------------

def ink_payload(room_code):
    """The drawing on the slide that is on screen now (None when no slides are up)."""
    live = SLIDES.get(room_code)
    if not live:
        return None
    return {"deck_id": live["deck_id"], "index": live["index"],
            "ops": cs.tools(room_code).ink.ops(live["deck_id"], live["index"])}


def send_ink_state(room_code, sid=None):
    payload = ink_payload(room_code)
    if payload is not None:
        socketio.emit("ink_state", payload, room=sid or room_code)


@socketio.on("ink_op")
def handle_ink_op(data):
    info = _host_info(_sid())
    live = SLIDES.get(info["room_code"]) if info else None
    if not live:
        return
    op = cs.clean_ink_op((data or {}).get("op"), cs.SLIDE_OPS)
    if op and cs.tools(info["room_code"]).ink.apply(live["deck_id"], live["index"], op):
        emit("ink_op", {"deck_id": live["deck_id"], "index": live["index"], "op": op},
             room=info["room_code"], include_self=False)


@socketio.on("laser")
def handle_laser(data):
    """The teacher's laser pointer. Never stored: it only exists while it moves."""
    info = _host_info(_sid())
    if not info or info["room_code"] not in SLIDES:
        return
    data = data or {}
    on = bool(data.get("on"))
    emit("laser", {"on": on, "x": round(cs._num(data.get("x"), 0, 1, 0), 4),
                   "y": round(cs._num(data.get("y"), 0, 1, 0), 4)},
         room=info["room_code"], include_self=False)


# ---------------------------------------------------------------------------
# Polls
# ---------------------------------------------------------------------------

def _poll_to_host(room_code):
    tools = cs.tools(room_code)
    if tools.poll:
        _to_host(room_code, "poll_host", tools.poll.host_view(_students(room_code)))


def _poll_to_everyone(room_code):
    """Each person gets their own view (their own answer; the right answer only once revealed)."""
    tools = cs.tools(room_code)
    for sid, info in list(CONNECTED.items()):
        if info["room_code"] != room_code or info["is_host"]:
            continue
        socketio.emit("poll_state", tools.poll.public_view(sid) if tools.poll else {"active": False}, room=sid)
    _poll_to_host(room_code)


def _poll_gone(room_code):
    for sid, info in list(CONNECTED.items()):
        if info["room_code"] == room_code:
            socketio.emit("poll_state", {"active": False}, room=sid)
    _to_host(room_code, "poll_host", {"active": False})


@socketio.on("poll_start")
def handle_poll_start(data):
    info = _host_info(_sid())
    if not info:
        return
    data = data or {}
    tools = cs.tools(info["room_code"])
    tools.poll_seq += 1
    tools.poll = cs.Poll(tools.poll_seq, data.get("q", ""), data.get("n", 4), data.get("correct"))
    _poll_to_everyone(info["room_code"])


@socketio.on("poll_answer")
def handle_poll_answer(data):
    sid = _sid()
    info = CONNECTED.get(sid)
    if not info or info["is_host"] or info.get("observer"):
        return
    tools = cs.peek(info["room_code"])
    poll = tools.poll if tools else None
    if not poll or (data or {}).get("id") != poll.id:
        return                                  # an answer to an old question
    if poll.answer(sid, (data or {}).get("choice")):
        emit("poll_state", poll.public_view(sid))
        _poll_to_host(info["room_code"])        # the teacher's live count


def _poll_action(fn):
    info = _host_info(_sid())
    tools = cs.peek(info["room_code"]) if info else None
    if info and tools and tools.poll:
        fn(tools.poll)
        _poll_to_everyone(info["room_code"])


@socketio.on("poll_close")
def handle_poll_close(data):
    _poll_action(lambda p: p.close())


@socketio.on("poll_reopen")
def handle_poll_reopen(data):
    def reopen(p):
        if not p.revealed:
            p.open = True
    _poll_action(reopen)


@socketio.on("poll_share")
def handle_poll_share(data):
    def share(p):
        p.shared = bool((data or {}).get("shared", True))
    _poll_action(share)


@socketio.on("poll_reveal")
def handle_poll_reveal(data):
    def reveal(p):
        if p.is_quiz:
            p.reveal()
    _poll_action(reveal)


@socketio.on("poll_end")
def handle_poll_end(data):
    info = _host_info(_sid())
    tools = cs.peek(info["room_code"]) if info else None
    if info and tools and tools.poll:
        tools.poll = None
        _poll_gone(info["room_code"])


# ---------------------------------------------------------------------------
# Breakout rooms
# ---------------------------------------------------------------------------

def _breakout_to_everyone(room_code):
    tools = cs.tools(room_code)
    names = _names(room_code)
    for sid, info in list(CONNECTED.items()):
        if info["room_code"] != room_code or info["is_host"]:
            continue
        socketio.emit("breakout_state", tools.breakouts.student_view(sid, names), room=sid)
    _to_host(room_code, "breakout_host", tools.breakouts.host_view(names))


def _auto_return(room_code, token):
    """Brings everyone back when the minutes run out (unless the teacher already did,
    or extended the time)."""
    while True:
        tools = cs.peek(room_code)
        if not tools or not tools.breakouts.active or tools.breakouts.token != token:
            return
        left = tools.breakouts.seconds_left()
        if left is None:
            return
        if left <= 0:
            _end_breakouts(room_code, "Time is up. Everyone is back in the main room.")
            return
        socketio.sleep(min(left, 5) + 0.2)


def _end_breakouts(room_code, message=None):
    tools = cs.peek(room_code)
    if not tools or not tools.breakouts.active:
        return
    tools.breakouts.end()
    socketio.emit("breakout_ended", {"message": message or "Back in the main room."}, room=room_code)
    _breakout_to_everyone(room_code)


@socketio.on("breakout_start")
def handle_breakout_start(data):
    info = _host_info(_sid())
    if not info:
        return
    room_code = info["room_code"]
    data = data or {}
    tools = cs.tools(room_code)
    mode = "pairs" if data.get("mode") == "pairs" else "groups"
    if not tools.breakouts.start(list(_students(room_code)), mode, data.get("value", 2), data.get("minutes", 0)):
        emit("breakout_error", {"message": "You need at least two students in the room to make groups."})
        return
    _breakout_to_everyone(room_code)
    if tools.breakouts.ends_at is not None:
        socketio.start_background_task(_auto_return, room_code, tools.breakouts.token)


@socketio.on("breakout_end")
def handle_breakout_end(data):
    info = _host_info(_sid())
    if info:
        _end_breakouts(info["room_code"])


@socketio.on("breakout_move")
def handle_breakout_move(data):
    info = _host_info(_sid())
    if not info:
        return
    room_code = info["room_code"]
    target = (data or {}).get("sid")
    group = _int_in_range((data or {}).get("group"), 1, cs.Breakouts.MAX_GROUPS)
    if target in _students(room_code) and group and cs.tools(room_code).breakouts.move(target, group):
        _breakout_to_everyone(room_code)


@socketio.on("breakout_visit")
def handle_breakout_visit(data):
    """Where the teacher's voice and picture go: one group, everyone, or nowhere."""
    info = _host_info(_sid())
    if not info:
        return
    where = (data or {}).get("where")
    if where not in (None, "all"):
        where = _int_in_range(where, 1, cs.Breakouts.MAX_GROUPS)
    if cs.tools(info["room_code"]).breakouts.set_visit(where):
        _breakout_to_everyone(info["room_code"])


@socketio.on("breakout_extend")
def handle_breakout_extend(data):
    info = _host_info(_sid())
    if info and cs.tools(info["room_code"]).breakouts.extend((data or {}).get("minutes", 1)):
        _breakout_to_everyone(info["room_code"])
        tools = cs.tools(info["room_code"])
        socketio.start_background_task(_auto_return, info["room_code"], tools.breakouts.token)


@socketio.on("breakout_broadcast")
def handle_breakout_broadcast(data):
    """A short message from the teacher to every group."""
    info = _host_info(_sid())
    text = str((data or {}).get("message", "")).strip()[:200]
    if info and text:
        socketio.emit("breakout_message", {"message": text}, room=info["room_code"])


@socketio.on("breakout_help")
def handle_breakout_help(data):
    sid = _sid()
    info = CONNECTED.get(sid)
    if not info or info["is_host"] or info.get("observer"):
        return
    tools = cs.peek(info["room_code"])
    group = tools.breakouts.group_of(sid) if tools else None
    if group:
        _to_host(info["room_code"], "breakout_help", {"group": group["id"], "name": group["name"], "from": info["name"]})


# ---------------------------------------------------------------------------
# Timer and "stay focused" bar
# ---------------------------------------------------------------------------

def _timer_to_room(room_code):
    socketio.emit("timer_state", cs.tools(room_code).timer.payload(), room=room_code)


@socketio.on("timer_start")
def handle_timer_start(data):
    info = _host_info(_sid())
    if not info:
        return
    data = data or {}
    cs.tools(info["room_code"]).timer.start(data.get("seconds", 300), data.get("label", ""), data.get("focus", False))
    _timer_to_room(info["room_code"])


@socketio.on("timer_control")
def handle_timer_control(data):
    info = _host_info(_sid())
    if not info:
        return
    timer = cs.tools(info["room_code"]).timer
    action = (data or {}).get("action")
    if action == "pause":
        timer.pause()
    elif action == "resume":
        timer.resume()
    elif action == "add":
        timer.add((data or {}).get("seconds", 60))
    elif action == "stop":
        timer.stop()
    else:
        return
    _timer_to_room(info["room_code"])


# ---------------------------------------------------------------------------
# People arriving, leaving, and the session ending
# ---------------------------------------------------------------------------

def on_join(sid, room_code):
    """Everything a person needs to see the room exactly as it is now."""
    info = CONNECTED.get(sid)
    if not info:
        return
    tools = cs.tools(room_code)
    board = tools.board
    if board.open or board.has_content():
        emit("wb_state", board.payload())
    send_ink_state(room_code, sid)
    timer = tools.timer.payload()
    if timer["active"]:
        emit("timer_state", timer)
    if info.get("observer"):
        return
    if info["is_host"]:
        if tools.poll:
            emit("poll_host", tools.poll.host_view(_students(room_code)))
        if tools.breakouts.active:
            emit("breakout_host", tools.breakouts.host_view(_names(room_code)))
        return
    if tools.poll:
        emit("poll_state", tools.poll.public_view(sid))
    if tools.breakouts.active:
        tools.breakouts.add_late(sid)
        _breakout_to_everyone(room_code)        # the group they joined also needs to know


def on_leave(sid, room_code):
    """A student left or lost connection."""
    tools = cs.peek(room_code)
    if not tools:
        return
    if tools.board.writer and tools.board.writer["sid"] == sid:
        tools.board.set_writer(None, None)
        socketio.emit("wb_writer", {"writer": None}, room=room_code)
    if tools.breakouts.active:
        tools.breakouts.remove(sid)
        _breakout_to_everyone(room_code)
    _poll_to_host(room_code)                    # the "waiting for" list changed


def on_slides_changed(room_code):
    """The slide on screen changed (presented, moved, or stopped): show its drawing."""
    send_ink_state(room_code)


def on_session_end(room_code):
    cs.drop(room_code)


def _sid():
    from flask import request
    return request.sid
