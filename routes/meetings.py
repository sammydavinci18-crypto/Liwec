import os
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    abort,
    jsonify,
    current_app,
    send_file,
)
from flask_login import login_required, current_user

import storage
from extensions import db, socketio
from models import Meeting, MeetingParticipant, ConsultationNote, Recording

meetings_bp = Blueprint("meetings", __name__)


# There is deliberately NO route here for creating a meeting or joining one by
# typing a room code. Meetings are created only by the admin (see
# routes/admin.py) — directly, or by approving a person's request — and a room
# can only be entered by the admin host or someone the admin has invited.

def _meeting_or_404(room_code):
    return Meeting.query.filter_by(room_code=room_code).first_or_404()


def _require_entry(meeting):
    """Aborts unless the current user may be in this meeting. Returns the
    entry state ("host" or "ok") otherwise. Uninvited users get a 404 so
    that a guessed room code reveals nothing."""
    state = meeting.entry_state(current_user)
    if state == "forbidden":
        abort(404)
    return state


@meetings_bp.route("/room/<room_code>")
@login_required
def room(room_code):
    meeting = _meeting_or_404(room_code)
    state = meeting.entry_state(current_user)

    if state == "forbidden":
        abort(404)
    if state == "cancelled":
        flash("This session was cancelled.", "info")
        return redirect(url_for("main.dashboard"))
    if state == "not_open":
        return render_template("meeting_not_open.html", meeting=meeting)

    # A meeting the admin has already ended: show playback instead of a live room.
    if meeting.status == "ended":
        if meeting.recording and meeting.recording.finalized:
            if meeting.recording.is_expired or meeting.recording.deleted_at:
                return render_template("meeting_ended.html", meeting=meeting, expired=True)
            return render_template("playback.html", meeting=meeting)
        return render_template("meeting_ended.html", meeting=meeting, expired=False)

    is_host = state == "host"

    if is_host:
        # Only the admin opening the room makes it live. A guest arriving
        # early just waits in the lobby (see sockets.py).
        if meeting.status != "live":
            meeting.status = "live"
            db.session.commit()
    else:
        # Record (or refresh) this guest's attendance.
        existing = MeetingParticipant.query.filter_by(
            meeting_id=meeting.id, user_id=current_user.id, left_at=None
        ).first()
        if not existing:
            db.session.add(MeetingParticipant(meeting_id=meeting.id, user_id=current_user.id))
            db.session.commit()

    call_limit_minutes = current_app.config["CALL_LIMIT_MINUTES"]
    return render_template(
        "meeting_room.html",
        meeting=meeting,
        is_host=is_host,
        is_observer=False,
        attendance_required=(not is_host) and meeting.attendance_required(),
        call_limit_minutes=call_limit_minutes,
        ice_servers=_ice_servers(),
        local_socketio=_local_socketio_available(),
    )


@meetings_bp.route("/room/<room_code>/observe")
@login_required
def observe(room_code):
    """Admin-only silent oversight of a LIVE session. The admin gets everyone's
    audio and video but sends nothing, is not listed to anyone, and leaves no
    attendance row. Anyone who isn't an admin gets a 404."""
    if not current_user.is_admin:
        abort(404)
    meeting = _meeting_or_404(room_code)
    if meeting.is_hosted_by(current_user):
        return redirect(url_for("meetings.room", room_code=room_code))
    if meeting.status != "live":
        flash("Only a live session can be observed.", "info")
        return redirect(url_for("admin.meeting_detail", meeting_id=meeting.id))

    # Admin-only audit trail (never shown in the room).
    current_app.logger.info(f"Admin {current_user.id} is observing meeting {meeting.id} ({meeting.room_code})")
    return render_template(
        "meeting_room.html",
        meeting=meeting,
        is_host=False,
        is_observer=True,
        attendance_required=False,
        call_limit_minutes=current_app.config["CALL_LIMIT_MINUTES"],
        ice_servers=_ice_servers(),
        local_socketio=_local_socketio_available(),
    )


def _ice_servers():
    cfg = current_app.config
    servers = []
    for url in cfg["ICE_SERVERS"]:
        entry = {"urls": url}
        if url.startswith("turn") and cfg.get("TURN_USERNAME"):
            entry["username"] = cfg["TURN_USERNAME"]
            entry["credential"] = cfg["TURN_CREDENTIAL"]
        servers.append(entry)
    return servers


def _local_socketio_available():
    """True if the Socket.IO browser library has been vendored into
    static/vendor/ (see tools/fetch_vendor.py), so the room can load it from
    this server instead of a public CDN."""
    return os.path.exists(os.path.join(current_app.static_folder, "vendor", "socket.io.min.js"))


@meetings_bp.route("/room/<room_code>/notes", methods=["GET", "POST"])
@login_required
def notes(room_code):
    meeting = _meeting_or_404(room_code)
    _require_entry(meeting)

    if request.method == "POST":
        content = request.form.get("content", "").strip()
        if content:
            note = ConsultationNote(meeting_id=meeting.id, author_id=current_user.id, content=content)
            db.session.add(note)
            db.session.commit()

            # Push it to everyone else in the room right away, so people
            # don't have to close/reopen the panel to see new notes.
            socketio.emit(
                "note_added",
                {
                    "author": note.author.name,
                    "content": note.content,
                    "created_at": note.created_at.strftime("%b %d, %Y %I:%M %p"),
                },
                room=meeting.room_code,
            )
        return jsonify(
            notes=[
                {
                    "author": n.author.name,
                    "content": n.content,
                    "created_at": n.created_at.strftime("%b %d, %Y %I:%M %p"),
                }
                for n in sorted(meeting.notes, key=lambda x: x.created_at)
            ]
        )

    return jsonify(
        notes=[
            {
                "author": n.author.name,
                "content": n.content,
                "created_at": n.created_at.strftime("%b %d, %Y %I:%M %p"),
            }
            for n in sorted(meeting.notes, key=lambda x: x.created_at)
        ]
    )


def _recording_key(meeting):
    return f"{meeting.id}.webm"


@meetings_bp.route("/room/<room_code>/recording/chunk", methods=["POST"])
@login_required
def upload_recording_chunk(room_code):
    """The host's browser POSTs the meeting recording here in ~30s chunks
    while the call is happening, so at most a few seconds are ever at risk
    if the host's browser crashes. Chunks are buffered on local disk for
    the duration of the live call only (Storage's simple upload API doesn't
    support appending) — the complete file gets pushed to Supabase Storage
    once, when the host ends the meeting. See end_meeting() below."""
    meeting = _meeting_or_404(room_code)
    if not meeting.is_hosted_by(current_user):
        abort(403)

    chunk = request.get_data()
    if not chunk:
        return jsonify(ok=True)

    key = _recording_key(meeting)
    storage.append_bytes(current_app.config["RECORDINGS_BUCKET"], key, chunk)

    recording = Recording.query.filter_by(meeting_id=meeting.id).first()
    if not recording:
        recording = Recording(meeting_id=meeting.id, filename=key, finalized=False)
        db.session.add(recording)
        db.session.commit()

    return jsonify(ok=True)


@meetings_bp.route("/room/<room_code>/end", methods=["POST"])
@login_required
def end_meeting(room_code):
    """Host ends the meeting: locks it as 'ended', uploads the completed
    recording to Storage (if any), and sets a retention expiry from the
    configured retention period."""
    meeting = _meeting_or_404(room_code)
    if not meeting.is_hosted_by(current_user):
        abort(403)

    meeting.status = "ended"
    meeting.ended_at = datetime.utcnow()

    recording = Recording.query.filter_by(meeting_id=meeting.id).first()
    bucket = current_app.config["RECORDINGS_BUCKET"]
    key = _recording_key(meeting)
    local_buffer = storage.local_path_if_exists(bucket, key)

    if recording and local_buffer:
        try:
            with open(local_buffer, "rb") as f:
                data = f.read()
            storage.upload_bytes(bucket, key, data, content_type="video/webm")
            # Only in Supabase mode is there a separate local buffer to
            # clean up — in local-fallback mode upload_bytes() just
            # rewrote the same file in place, so this is a no-op there.
            if current_app.config.get("SUPABASE_URL") and current_app.config.get("SUPABASE_SERVICE_KEY"):
                os.remove(local_buffer)
            recording.finalized = True
            retention_days = current_app.config["RECORDING_RETENTION_DAYS"]
            # 0 = keep until the admin deletes it.
            recording.expires_at = (
                datetime.utcnow() + timedelta(days=retention_days) if retention_days > 0 else None
            )
        except storage.StorageError as exc:
            current_app.logger.error(f"Recording upload failed for meeting {meeting.id}: {exc}")

    db.session.commit()

    # Tell everyone still in the room (and anyone waiting in the lobby) the
    # session is over, and forget who was admitted to it.
    import sockets
    socketio.emit("meeting_ended", {}, room=meeting.room_code)
    for waiting_sid in list(sockets.WAITING.pop(meeting.room_code, {})):
        socketio.emit("meeting_ended", {}, room=waiting_sid)
    sockets.ADMITTED.pop(meeting.room_code, None)
    sockets.OBSERVERS.pop(meeting.room_code, None)
    sockets.WHITEBOARD.pop(meeting.room_code, None)
    sockets.RECORDING.pop(meeting.room_code, None)

    return jsonify(ok=True, redirect=url_for("main.dashboard"))


@meetings_bp.route("/recordings/<int:meeting_id>/file")
@login_required
def stream_recording(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    recording = meeting.recording

    if not recording or not recording.finalized:
        abort(404)

    # Only the admin host, or a guest the admin invited, can watch it back.
    if meeting.entry_state(current_user) not in ("host", "ok"):
        abort(403)

    if recording.is_expired or recording.deleted_at:
        abort(410)  # Gone — matches meeting_ended.html's "expired" messaging

    bucket = current_app.config["RECORDINGS_BUCKET"]
    url = storage.signed_url(bucket, recording.filename, expires_in=120)
    if url:
        return redirect(url)

    # Local-fallback mode.
    local_dir = current_app.config["RECORDINGS_DIR"]
    path = os.path.join(local_dir, recording.filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="video/webm", conditional=True)
