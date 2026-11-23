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

import rtc
import storage
import recording_service
from extensions import db, socketio
from models import Meeting, MeetingParticipant, ConsultationNote, Recording, generate_guest_token

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
        is_guest=False,
        viewer_name=current_user.name,
        home_url=url_for("main.dashboard"),
        guest_url=(url_for("guest.join", token=meeting.guest_token, _external=True)
                   if is_host and meeting.guest_token else None),
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
        is_guest=False,
        viewer_name=current_user.name,
        home_url=url_for("main.dashboard"),
        attendance_required=False,
        call_limit_minutes=current_app.config["CALL_LIMIT_MINUTES"],
        ice_servers=_ice_servers(),
        local_socketio=_local_socketio_available(),
    )


def _ice_servers():
    return rtc.ice_servers()


@meetings_bp.app_context_processor
def _room_rtc_context():
    """Extra settings every room page needs (see templates/meeting_room.html)."""
    cfg = current_app.config
    return {
        "livekit_url": cfg["LIVEKIT_URL"] if rtc.livekit_enabled(cfg) else "",
        "turn_missing": not rtc.turn_configured(cfg) and not rtc.livekit_enabled(cfg),
        "local_livekit": os.path.exists(os.path.join(current_app.static_folder, "vendor", "livekit-client.umd.min.js")),
    }


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
            # Session notes are for account holders only: guests who came in
            # through the share link never receive them.
            import sockets
            sockets.emit_to_members(
                meeting.room_code,
                "note_added",
                {
                    "author": note.author.name,
                    "content": note.content,
                    "created_at": note.created_at.strftime("%b %d, %Y %I:%M %p"),
                },
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


@meetings_bp.route("/room/<room_code>/recording/status")
@login_required
def recording_status(room_code):
    """Tells the host's browser how many chunks the server has, so after a
    reload or a dropped connection it can resend exactly what is missing."""
    meeting = _meeting_or_404(room_code)
    if not meeting.is_hosted_by(current_user):
        abort(403)
    rec = Recording.query.filter_by(meeting_id=meeting.id).first()
    return jsonify(ok=True, received=(rec.chunks_received if rec and rec.source == "browser" else 0),
                   source=(rec.source if rec else None))


@meetings_bp.route("/room/<room_code>/recording/chunk", methods=["POST"])
@login_required
def upload_recording_chunk(room_code):
    """Browser recording, used when the server can't record (or while it is
    not configured). The host's browser POSTs ~10s chunks, numbered from 0.

    Numbering makes retries safe: a chunk the server already has is simply
    acknowledged (never appended twice), and a chunk that arrives ahead of a
    missing one is refused with the number the server needs next, so the file
    can never be assembled out of order. Chunks are buffered on local disk for
    the live call only (Storage's simple upload API has no append); the whole
    file is pushed to Supabase once, when the session ends (finish_meeting)."""
    meeting = _meeting_or_404(room_code)
    if not meeting.is_hosted_by(current_user):
        abort(403)
    if meeting.status in ("ended", "cancelled"):
        return jsonify(ok=False, error="This session has ended."), 410

    chunk = request.get_data()
    if not chunk:
        return jsonify(ok=True)

    recording = Recording.query.filter_by(meeting_id=meeting.id).first()
    key = _recording_key(meeting)
    if not recording:
        recording = Recording(meeting_id=meeting.id, filename=key, finalized=False, source="browser")
        db.session.add(recording)
        db.session.flush()
    elif recording.source != "browser":
        # The server recorder gave up (or was never available): the browser takes over.
        recording.source, recording.filename, recording.egress_id = "browser", key, None
        recording.chunks_received = 0

    seq_raw = request.args.get("seq")
    expected = recording.chunks_received or 0
    if seq_raw is not None:
        try:
            seq = int(seq_raw)
        except ValueError:
            return jsonify(ok=False, error="bad seq"), 400
        if seq < expected:
            db.session.commit()
            return jsonify(ok=True, duplicate=True, expected=expected)
        if seq > expected:
            db.session.commit()
            return jsonify(ok=False, expected=expected), 409

    storage.append_bytes(current_app.config["RECORDINGS_BUCKET"], key, chunk)
    recording.chunks_received = expected + 1
    db.session.commit()
    return jsonify(ok=True, expected=recording.chunks_received)


def finish_meeting(meeting):
    """Ends a session: locks it as 'ended', moves the finished recording into
    Storage (if there is one) and sets its retention expiry, then tells
    everyone still in the room or the lobby. Used by the host's End button and
    by the admin's "close and save" for a session whose host vanished."""
    meeting.status = "ended"
    meeting.ended_at = datetime.utcnow()

    recording = Recording.query.filter_by(meeting_id=meeting.id).first()
    bucket = current_app.config["RECORDINGS_BUCKET"]
    key = _recording_key(meeting)
    local_buffer = storage.local_path_if_exists(bucket, key)
    server_recorded = bool(recording and recording.source == "server" and recording.egress_id)

    if server_recorded:
        # The media server finishes and uploads the file itself; ending is
        # instant, and the recording turns "Saved" when the file has landed.
        db.session.commit()
        app_obj = current_app._get_current_object()
        socketio.start_background_task(recording_service.stop_and_finalize, app_obj, meeting.id)
    elif recording and local_buffer:
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

    import sockets
    socketio.emit("meeting_ended", {}, room=meeting.room_code)
    for waiting_sid in list(sockets.WAITING.pop(meeting.room_code, {})):
        socketio.emit("meeting_ended", {}, room=waiting_sid)
    sockets.ADMITTED.pop(meeting.room_code, None)
    sockets.OBSERVERS.pop(meeting.room_code, None)
    sockets.clear_slides(meeting.room_code)
    sockets.sockets_tools.on_session_end(meeting.room_code)   # whiteboard, drawing, poll, groups, timer
    sockets.RECORDING.pop(meeting.room_code, None)
    sockets.CALLON.pop(meeting.room_code, None)

    # Disconnect everyone from the media server too (background: ending stays instant).
    # (With a server recorder this happens right after the recorder is stopped.)
    if rtc.livekit_enabled() and not server_recorded:
        app_obj = current_app._get_current_object()
        def _close_media_room(app, room):
            with app.app_context():
                rtc.delete_room(room)
        socketio.start_background_task(_close_media_room, app_obj, meeting.room_code)

    # The slide pictures are no longer needed (the recording holds what the
    # class saw): free the storage, in the background so ending is instant.
    import slides
    socketio.start_background_task(
        slides.purge_meeting_decks, current_app._get_current_object(), meeting.id
    )


@meetings_bp.route("/room/<room_code>/end", methods=["POST"])
@login_required
def end_meeting(room_code):
    """Host ends the meeting (this also stops the recording)."""
    meeting = _meeting_or_404(room_code)
    if not meeting.is_hosted_by(current_user):
        abort(403)
    finish_meeting(meeting)
    return jsonify(ok=True, redirect=url_for("main.dashboard"))


# ---------------------------------------------------------------------------
# The shareable student link. Only the host of a session manages it.
# ---------------------------------------------------------------------------

def guest_link_payload(meeting):
    return {
        "ok": True,
        "url": url_for("guest.join", token=meeting.guest_token, _external=True),
        "open": bool(meeting.guest_link_open),
        "auto_admit": bool(meeting.guest_auto_admit),
    }


@meetings_bp.route("/room/<room_code>/guest-link", methods=["POST"])
@login_required
def guest_link_settings(room_code):
    """Host controls for the student link: let guests walk in / admit each one,
    stop new people using the link, or replace it with a fresh one."""
    meeting = _meeting_or_404(room_code)
    if not meeting.is_hosted_by(current_user):
        abort(403)
    if meeting.status in ("ended", "cancelled"):
        return jsonify(ok=False, error="This session is over."), 400

    action = request.form.get("action")
    if action == "auto_admit_on":
        meeting.guest_auto_admit = True
    elif action == "auto_admit_off":
        meeting.guest_auto_admit = False
    elif action == "close":
        meeting.guest_link_open = False
    elif action == "open":
        meeting.guest_link_open = True
    elif action == "regenerate":
        meeting.guest_token = generate_guest_token()
        meeting.guest_link_open = True
    else:
        return jsonify(ok=False, error="Unknown action."), 400
    db.session.commit()

    if meeting.guest_auto_admit:
        import sockets
        sockets.admit_waiting_guests(meeting.room_code)
    return jsonify(guest_link_payload(meeting))


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
    return send_file(path, mimetype=recording_service.content_type_for(recording.filename), conditional=True)
