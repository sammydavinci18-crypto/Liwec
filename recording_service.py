"""Who records a lesson, and what happens if that recorder fails.

Preferred: the SERVER records (LiveKit Egress, see rtc.py). The teacher's
laptop does nothing extra, so a slow machine can't produce a poor recording and
a browser crash can't lose it.

Fallback: the host's browser records (static/js/recorder.js), as before but
hardened. The server decides which one is in charge and tells the host's
browser with a `recording_mode` event; a lesson is never left unrecorded:

    host joins ──► server recording starts ok ──► mode "server"
                └► not configured / failed    ──► mode "browser"
    server recorder dies mid-lesson           ──► mode "browser" (pushed live)
"""
import time
from datetime import datetime, timedelta

import rtc
import storage
from extensions import db, socketio
from models import Meeting, Recording

POLL_SECONDS = 10


def recording_key(meeting, source):
    return f"{meeting.id}.mp4" if source == "server" else f"{meeting.id}.webm"


def content_type_for(filename):
    return "video/mp4" if filename.endswith(".mp4") else "video/webm"


def begin(app, meeting_id, host_sid):
    """Called (in the background) when the host enters the room. Decides the
    recording mode and tells the host's browser."""
    with app.app_context():
        meeting = Meeting.query.get(meeting_id)
        if meeting is None:
            return
        mode, reason = "browser", "not configured"

        if app.config.get("RECORD_TEACHER_WINDOW", True):
            reason = "recording the teacher's window"
        elif rtc.egress_enabled():
            try:
                rec = Recording.query.filter_by(meeting_id=meeting.id).first()
                # The host reloaded: the server recorder is still running.
                if rec and rec.source == "server" and rec.egress_id and rtc.egress_status(rec.egress_id, meeting.room_code) == "active":
                    mode = "server"
                elif rec and rec.source == "browser" and rec.chunks_received:
                    mode, reason = "browser", "already recording in the browser"
                else:
                    key = recording_key(meeting, "server")
                    egress_id = rtc.start_egress(meeting, key)
                    if rec is None:
                        rec = Recording(meeting_id=meeting.id, filename=key, finalized=False)
                        db.session.add(rec)
                    rec.source, rec.egress_id, rec.filename = "server", egress_id, key
                    db.session.commit()
                    mode = "server"
                    socketio.start_background_task(_watch, app, meeting.id)
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                reason = f"server recorder unavailable: {exc}"
                app.logger.warning("Server recording failed to start for meeting %s: %s", meeting_id, exc)

        socketio.emit("recording_mode", {"mode": mode, "reason": reason if mode == "browser" else ""}, room=host_sid)


def _watch(app, meeting_id):
    """While the lesson is live, make sure the server recorder is still alive.
    If it died, hand recording to the host's browser straight away."""
    time.sleep(POLL_SECONDS * 2)
    while True:
        with app.app_context():
            meeting = Meeting.query.get(meeting_id)
            rec = Recording.query.filter_by(meeting_id=meeting_id).first()
            if meeting is None or meeting.status != "live" or rec is None or rec.source != "server":
                return
            status = rtc.egress_status(rec.egress_id, meeting.room_code)
            if status == "failed":
                app.logger.error("Server recorder for meeting %s failed; switching to the host's browser", meeting_id)
                rec.source = "browser"
                rec.filename = recording_key(meeting, "browser")
                rec.egress_id = None
                db.session.commit()
                import sockets
                host_sid = sockets._host_sid_for_room(meeting.room_code)
                if host_sid:
                    socketio.emit("recording_mode", {"mode": "browser", "reason": "the server recorder stopped"}, room=host_sid)
                return
        time.sleep(POLL_SECONDS)


def stop_and_finalize(app, meeting_id):
    """The lesson ended. Stop the server recorder, wait for the file to be
    written to storage, then mark the recording ready to watch."""
    with app.app_context():
        meeting = Meeting.query.get(meeting_id)
        rec = Recording.query.filter_by(meeting_id=meeting_id).first()
        if meeting is None or rec is None or rec.source != "server" or not rec.egress_id:
            return
        rtc.stop_egress(rec.egress_id, meeting.room_code)
        rtc.delete_room(meeting.room_code)  # now that the recorder has been told to finish

        bucket = app.config["RECORDINGS_BUCKET"]
        deadline = time.time() + 15 * 60
        done = False
        while time.time() < deadline:
            status = rtc.egress_status(rec.egress_id, meeting.room_code)
            if status == "complete":
                done = True
                break
            if status == "failed":
                break
            # Completed egress may no longer be listed: the file itself settles it.
            if status == "unknown" and storage.object_exists(bucket, rec.filename):
                done = True
                break
            time.sleep(5)

        rec = Recording.query.filter_by(meeting_id=meeting_id).first()
        if done and storage.object_exists(bucket, rec.filename):
            rec.finalized = True
            days = app.config["RECORDING_RETENTION_DAYS"]
            rec.expires_at = datetime.utcnow() + timedelta(days=days) if days > 0 else None
            rec.ensure_share_token()
            db.session.commit()
        else:
            app.logger.error("Server recording for meeting %s did not finish", meeting_id)


# ---------------------------------------------------------------------------
# Saving a browser recording (runs in the background, never inside the request
# that ends the lesson)
# ---------------------------------------------------------------------------
import os
import shutil
import subprocess

FINALIZING = set()      # meeting ids being saved right now
_HOST_LEFT = {}         # room_code -> time.time() the host's connection dropped
MIN_USABLE_BYTES = 2048


def is_saving(meeting_id):
    return meeting_id in FINALIZING


def _remux_webm(path):
    """MediaRecorder files have no duration or seek index, so the player can't
    skip around. If ffmpeg is installed, rewrite the container (no re-encode,
    takes seconds). Best effort: any problem leaves the original untouched."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    out = str(path) + ".fixed.webm"
    try:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-fflags", "+genpts", "-i", str(path), "-c", "copy", out],
            check=True, timeout=900, stdin=subprocess.DEVNULL,
        )
        if os.path.getsize(out) > MIN_USABLE_BYTES:
            os.replace(out, path)
            return True
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        if os.path.exists(out):
            os.remove(out)
    except OSError:
        pass
    return False


def finalize_browser_recording(app, meeting_id):
    """Turn the chunks the host's browser sent into a finished, watchable
    recording. Safe to call again (the admin's Retry button does): it picks up
    wherever the file is. It NEVER leaves a received recording unsaved:
    if cloud storage refuses the file, it is kept on the server's disk and is
    played from there, with a note for the admin."""
    with app.app_context():
        if meeting_id in FINALIZING:
            return
        FINALIZING.add(meeting_id)
        try:
            _finalize_browser(app, meeting_id)
        except Exception:  # noqa: BLE001
            db.session.rollback()
            app.logger.exception("Saving the recording for meeting %s crashed", meeting_id)
        finally:
            FINALIZING.discard(meeting_id)


def _finalize_browser(app, meeting_id):
    rec = Recording.query.filter_by(meeting_id=meeting_id).first()
    if rec is None or rec.source != "browser":
        return
    bucket = app.config["RECORDINGS_BUCKET"]
    key = rec.filename
    cloud = bool(app.config.get("SUPABASE_URL") and app.config.get("SUPABASE_SERVICE_KEY"))
    buf = storage.local_path_if_exists(bucket, key)

    if buf is None:
        # Nothing on disk: either it was already uploaded, or nothing ever arrived.
        if cloud and storage.object_exists(bucket, key):
            _mark_saved(app, rec, note=None, size=rec.size_bytes)
        else:
            rec.save_note = ("No video data reached the server. The teacher's browser never sent the recording "
                             "(or the server's disk was cleared before it was saved).")
            db.session.commit()
            app.logger.error("Meeting %s: no recording data to save (chunks_received=%s)", meeting_id, rec.chunks_received)
        return

    size = os.path.getsize(buf)
    if size < MIN_USABLE_BYTES:
        rec.save_note = f"Only {size} bytes were received, too little to be a video."
        db.session.commit()
        app.logger.error("Meeting %s: recording too small (%s bytes)", meeting_id, size)
        return

    if _remux_webm(buf):
        size = os.path.getsize(buf)

    if not cloud:
        _mark_saved(app, rec, note=None, size=size)
        app.logger.info("Meeting %s: recording saved on the server's disk (%s bytes)", meeting_id, size)
        return

    last_error = None
    for attempt in range(1, 4):
        try:
            storage.upload_file(bucket, key, str(buf), content_type=content_type_for(key))
            os.remove(buf)
            _mark_saved(app, rec, note=None, size=size)
            app.logger.info("Meeting %s: recording uploaded to cloud storage (%s bytes)", meeting_id, size)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            app.logger.warning("Meeting %s: upload attempt %s failed: %s", meeting_id, attempt, exc)
            time.sleep(5 * attempt)

    # Cloud storage would not take it. Keep it: play it from this server's disk.
    _mark_saved(app, rec, size=size, note=(
        "Cloud storage refused the file, so it is kept on this server's disk and played from there "
        f"({str(last_error)[:160]}). Files over the storage limit (50 MB on Supabase's free plan) are refused: "
        "raise the limit in Supabase > Storage > Settings, or use a plan with a higher one, then press Retry."
    ))
    app.logger.error("Meeting %s: cloud upload failed, kept locally: %s", meeting_id, last_error)


def _mark_saved(app, rec, note, size):
    rec.finalized = True
    rec.save_note = note
    rec.size_bytes = size
    days = app.config["RECORDING_RETENTION_DAYS"]
    rec.expires_at = datetime.utcnow() + timedelta(days=days) if days > 0 else None
    rec.ensure_share_token()
    db.session.commit()


def open_recording_response(recording, bucket, local_dir_unused=None):
    """A response that plays this recording: from the server's disk if the file
    is there (supports seeking), otherwise a redirect to a signed cloud URL.
    Never raises: a storage hiccup is a 404/503 for the video, not a crashed page."""
    import requests
    from flask import abort, current_app, redirect, send_file
    local = storage.local_path_if_exists(bucket, recording.filename)
    if local is not None:
        try:
            return send_file(os.path.abspath(str(local)), mimetype=content_type_for(recording.filename), conditional=True)
        except OSError:
            current_app.logger.exception("Could not read local recording file %s", local)
            if not (current_app.config.get("SUPABASE_URL") and current_app.config.get("SUPABASE_SERVICE_KEY")):
                abort(404)
    try:
        url = storage.signed_url(bucket, recording.filename, expires_in=6 * 3600)
    except storage.StorageError:
        current_app.logger.warning("No signed URL for recording %s", recording.filename)
        abort(404)
    except requests.RequestException:
        current_app.logger.exception("Storage unreachable while opening recording %s", recording.filename)
        abort(503)
    if url:
        return redirect(url)
    abort(404)


_SCHEMA_OK = False


def ensure_schema():
    """The recordings table must have every column the model expects. A database
    that missed a migration used to make EVERY recordings page return error 500
    (the query itself fails). This adds any missing column (additive only, never
    drops or changes data) once per server start, and never raises."""
    global _SCHEMA_OK
    if _SCHEMA_OK:
        return
    try:
        from sqlalchemy import inspect, text
        engine = db.engine
        if "recordings" not in inspect(engine).get_table_names():
            db.create_all()
            _SCHEMA_OK = True
            return
        have = {c["name"] for c in inspect(engine).get_columns("recordings")}
        quote = engine.dialect.identifier_preparer.quote
        for col in Recording.__table__.columns:
            if col.name in have:
                continue
            ddl = f"ALTER TABLE recordings ADD COLUMN {quote(col.name)} {col.type.compile(dialect=engine.dialect)}"
            default = {"source": "'browser'", "chunks_received": "0", "share_enabled": "true"}.get(col.name)
            if default is not None:
                ddl += f" DEFAULT {default}" + ("" if col.nullable else " NOT NULL")
            with engine.begin() as conn:
                conn.execute(text(ddl))
            from flask import current_app
            current_app.logger.warning("Added missing column recordings.%s", col.name)
        _SCHEMA_OK = True
    except Exception:  # noqa: BLE001
        db.session.rollback()
        from flask import current_app
        current_app.logger.exception("Could not verify the recordings table")


def safe_recording_state(meeting):
    """recording_state() that can never break a page."""
    try:
        return recording_state(meeting)
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return "none"


def prepare_recordings_page(app, meetings_query):
    """Shared by the admin's and the teacher's Recordings pages: make sure the
    table is healthy, close abandoned sessions, run the query, and give older
    recordings a watch link. Each step is isolated so one bad record cannot
    turn the whole page into an error 500."""
    ensure_schema()
    try:
        close_abandoned(app)
    except Exception:  # noqa: BLE001
        db.session.rollback()
        app.logger.exception("close_abandoned failed (page still shown)")
    ended = meetings_query().all()
    try:
        ensure_share_tokens(ended)
    except Exception:  # noqa: BLE001
        db.session.rollback()
        app.logger.exception("ensure_share_tokens failed (page still shown)")
        ended = meetings_query().all()
    return ended


# ---------------------------------------------------------------------------
# A host who vanished (closed the tab, lost power): end the session for them
# after a grace period so the recording is saved instead of left hanging.
# ---------------------------------------------------------------------------

def host_dropped(app, meeting_id, room_code):
    minutes = app.config.get("HOST_GONE_AUTO_END_MINUTES", 10)
    if minutes <= 0:
        return
    _HOST_LEFT[room_code] = time.time()
    socketio.start_background_task(_auto_end_if_still_gone, app, meeting_id, room_code, minutes * 60)


def host_back(room_code):
    _HOST_LEFT.pop(room_code, None)


def _auto_end_if_still_gone(app, meeting_id, room_code, delay):
    time.sleep(delay)
    with app.app_context():
        import sockets
        left = _HOST_LEFT.get(room_code)
        if left is None or time.time() - left < delay - 2:
            return  # the host came back (and maybe dropped again later: that has its own timer)
        meeting = Meeting.query.get(meeting_id)
        if meeting is None or meeting.status != "live" or sockets._host_sid_for_room(room_code):
            return
        app.logger.warning("Host of meeting %s has been gone %s min: ending it and saving the recording", meeting_id, delay // 60)
        from routes.meetings import finish_meeting
        finish_meeting(meeting)
        _HOST_LEFT.pop(room_code, None)


def close_abandoned(app):
    """Safety net, run when someone opens a recordings page: a session still
    'live' with nobody hosting it long after it should have finished (for
    example the server restarted mid-lesson) is closed and saved."""
    import sockets
    from routes.meetings import finish_meeting
    now = datetime.utcnow()
    closed = 0
    for m in Meeting.query.filter(Meeting.status == "live").all():
        if sockets._host_sid_for_room(m.room_code):
            continue
        try:
            end = m.ends_at() or ((m.call_started_at or m.created_at or now) + timedelta(hours=4))
            if now > end + timedelta(minutes=20):
                finish_meeting(m)
                closed += 1
        except Exception:  # noqa: BLE001 - one odd session must not stop the rest
            db.session.rollback()
    return closed


def recording_state(meeting):
    """One word for how a finished session's recording is doing:
    ready | expired | saving | failed (arrived but not saved) | none (nothing arrived)."""
    rec = meeting.recording
    if rec is None:
        return "none"
    if rec.finalized:
        return "expired" if (rec.is_expired or rec.deleted_at) else "ready"
    if meeting.id in FINALIZING:
        return "saving"
    # A server recorder that was told to stop is still writing its file.
    if rec.source == "server" and meeting.ended_at and (datetime.utcnow() - meeting.ended_at) < timedelta(minutes=16):
        return "saving"
    return "failed" if (rec.chunks_received or rec.source == "server") else "none"


def ensure_share_tokens(meetings):
    """Older recordings were saved before watch links existed: give them one."""
    changed = False
    for m in meetings:
        r = m.recording
        if r and r.finalized and not r.share_token:
            r.ensure_share_token()
            changed = True
    if changed:
        db.session.commit()
