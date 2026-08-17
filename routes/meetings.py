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
from models import Meeting, MeetingParticipant, ConsultationNote, Recording, User

meetings_bp = Blueprint("meetings", __name__)


@meetings_bp.route("/meetings/new", methods=["GET", "POST"])
@login_required
def create_meeting():
    if request.method == "POST":
        title = request.form.get("title", "").strip() or "Consultation"
        scheduled_raw = request.form.get("scheduled_time", "").strip()
        scheduled_time = None
        if scheduled_raw:
            try:
                scheduled_time = datetime.fromisoformat(scheduled_raw)
            except ValueError:
                scheduled_time = None

        meeting = Meeting(title=title, host_id=current_user.id, scheduled_time=scheduled_time)
        db.session.add(meeting)
        db.session.commit()

        flash("Meeting created. Share the room code with your participants.", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("create_meeting.html")


@meetings_bp.route("/join", methods=["GET", "POST"])
@login_required
def join_meeting():
    if request.method == "POST":
        code = request.form.get("room_code", "").strip()
        meeting = Meeting.query.filter_by(room_code=code).first()
        if not meeting:
            flash("No meeting found with that room code.", "error")
            return render_template("join_meeting.html")
        return redirect(url_for("meetings.room", room_code=meeting.room_code))

    return render_template("join_meeting.html")


@meetings_bp.route("/room/<room_code>")
@login_required
def room(room_code):
    meeting = Meeting.query.filter_by(room_code=room_code).first_or_404()

    # A meeting the host has already ended: show playback instead of a live room.
    if meeting.status == "ended":
        if meeting.recording and meeting.recording.finalized:
            if meeting.recording.is_expired or meeting.recording.deleted_at:
                return render_template("meeting_ended.html", meeting=meeting, expired=True)
            return render_template("playback.html", meeting=meeting)
        return render_template("meeting_ended.html", meeting=meeting, expired=False)

    # Record (or refresh) this user's participation, unless they're the host
    if meeting.host_id != current_user.id:
        existing = MeetingParticipant.query.filter_by(
            meeting_id=meeting.id, user_id=current_user.id, left_at=None
        ).first()
        if not existing:
            db.session.add(MeetingParticipant(meeting_id=meeting.id, user_id=current_user.id))
            db.session.commit()

    meeting.status = "live"
    db.session.commit()

    is_host = meeting.host_id == current_user.id
    call_limit_minutes = current_app.config["PREMIUM_CALL_LIMIT_MINUTES"] if meeting.host.is_premium \
        else current_app.config["FREE_CALL_LIMIT_MINUTES"]
    return render_template(
        "meeting_room.html", meeting=meeting, is_host=is_host, call_limit_minutes=call_limit_minutes
    )


@meetings_bp.route("/room/<room_code>/notes", methods=["GET", "POST"])
@login_required
def notes(room_code):
    meeting = Meeting.query.filter_by(room_code=room_code).first_or_404()

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
    meeting = Meeting.query.filter_by(room_code=room_code).first_or_404()
    if meeting.host_id != current_user.id:
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
    recording to Storage (if any), and sets a retention expiry based on the
    host's plan at the time."""
    meeting = Meeting.query.filter_by(room_code=room_code).first_or_404()
    if meeting.host_id != current_user.id:
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
            retention_days = (
                current_app.config["PREMIUM_RECORDING_RETENTION_DAYS"] if meeting.host.is_premium
                else current_app.config["FREE_RECORDING_RETENTION_DAYS"]
            )
            recording.expires_at = datetime.utcnow() + timedelta(days=retention_days)
        except storage.StorageError as exc:
            current_app.logger.error(f"Recording upload failed for meeting {meeting.id}: {exc}")

    db.session.commit()
    return jsonify(ok=True, redirect=url_for("main.dashboard"))


@meetings_bp.route("/recordings/<int:meeting_id>/file")
@login_required
def stream_recording(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    recording = meeting.recording

    if not recording or not recording.finalized:
        abort(404)

    # Only the host or someone who actually attended can watch it back.
    is_host = meeting.host_id == current_user.id
    was_participant = MeetingParticipant.query.filter_by(
        meeting_id=meeting.id, user_id=current_user.id
    ).first() is not None
    if not (is_host or was_participant):
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
