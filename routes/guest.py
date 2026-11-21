"""The shareable student link. A teacher (or the admin) copies a lesson's link
and sends it wherever the students are; opening it asks for a name and goes
straight into the lesson. No account, no password.

What this does NOT open up: the room code alone is useless without the browser
session this module creates, and a link only ever leads into the one session it
belongs to. Everything else (recording, whiteboard, host controls, admin
observe) is unchanged; guests are simply another kind of participant."""
import re
from datetime import datetime

from flask import (
    Blueprint, render_template, redirect, url_for, request, session, abort, current_app,
)
from flask_login import current_user

from extensions import db
from models import Meeting, MeetingGuest
from routes.meetings import _ice_servers, _local_socketio_available

guest_bp = Blueprint("guest", __name__)

MAX_NAME_LENGTH = 40


def _clean_name(raw):
    name = re.sub(r"[\x00-\x1f\x7f]", "", raw or "")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:MAX_NAME_LENGTH]


def session_guest(meeting):
    """The MeetingGuest this browser registered for `meeting`, or None."""
    gid = (session.get("guests") or {}).get(meeting.room_code)
    if not gid:
        return None
    guest = MeetingGuest.query.get(gid)
    return guest if guest and guest.meeting_id == meeting.id else None


def _remember(meeting, guest):
    guests = dict(session.get("guests") or {})
    guests[meeting.room_code] = guest.id
    session["guests"] = guests


def _closed(state, meeting=None, status=200):
    return render_template("guest/closed.html", state=state, meeting=meeting), status


@guest_bp.route("/join/<token>", methods=["GET", "POST"])
def join(token):
    meeting = Meeting.query.filter_by(guest_token=token).first()
    if meeting is None:
        return _closed("invalid", status=404)

    # People with an account who already belong in this session (the host, or
    # someone the admin invited) use their normal door.
    if current_user.is_authenticated and meeting.entry_state(current_user) in ("host", "ok"):
        return redirect(url_for("meetings.room", room_code=meeting.room_code))

    existing = session_guest(meeting)
    if existing and existing.removed_at:
        return _closed("removed", meeting)

    state = meeting.guest_link_state()
    if state != "ok":
        # A guest who is already in may come back even if the link has since
        # been closed (a dropped connection shouldn't lock a student out).
        if not (existing and state == "closed"):
            return _closed(state, meeting)

    if request.method == "POST":
        name = _clean_name(request.form.get("name"))
        if len(name) < 2:
            return render_template("guest/join.html", meeting=meeting, name=name,
                                   error="Please type your name so your teacher knows who you are.")
        import sockets
        limit = current_app.config["GUEST_LIMIT"]
        if limit and sockets.connected_guest_count(meeting.room_code) >= limit:
            return _closed("full", meeting)

        guest = existing if (existing and existing.name == name) else None
        if guest is None:
            guest = MeetingGuest(meeting_id=meeting.id, name=name)
            db.session.add(guest)
            db.session.commit()
        _remember(meeting, guest)
        return redirect(url_for("guest.room", room_code=meeting.room_code))

    return render_template("guest/join.html", meeting=meeting,
                           name=existing.name if existing else "", error=None)


@guest_bp.route("/g/<room_code>")
def room(room_code):
    meeting = Meeting.query.filter_by(room_code=room_code).first_or_404()
    guest = session_guest(meeting)
    if guest is None:
        # Not registered in this browser: send them through the name form.
        if meeting.guest_token:
            return redirect(url_for("guest.join", token=meeting.guest_token))
        abort(404)
    if guest.removed_at:
        return _closed("removed", meeting)
    if meeting.status in ("ended", "cancelled"):
        return _closed(meeting.status, meeting)

    return render_template(
        "meeting_room.html",
        meeting=meeting,
        is_host=False,
        is_observer=False,
        is_guest=True,
        viewer_name=guest.name,
        home_url=url_for("guest.join", token=meeting.guest_token) if meeting.guest_token else "/",
        attendance_required=meeting.attendance_required(),
        call_limit_minutes=current_app.config["CALL_LIMIT_MINUTES"],
        ice_servers=_ice_servers(),
        local_socketio=_local_socketio_available(),
    )
