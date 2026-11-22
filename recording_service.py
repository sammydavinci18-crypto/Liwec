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

        if rtc.egress_enabled():
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
            db.session.commit()
        else:
            app.logger.error("Server recording for meeting %s did not finish", meeting_id)
