"""Watching a recording, and sharing it.

The admin or the teacher who hosted a lesson copies its WATCH LINK
(/watch/<token>) and sends it to students anywhere. Opening it plays the whole
lesson from the start, like being in the room: no account, no name, no
password. The token is long and random; the host can switch the link off or
replace it, and it stops working when the recording expires.
"""
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

import recording_service
from extensions import db
from models import Meeting, Recording

watch_bp = Blueprint("watch", __name__)


def _closed(reason, status=200):
    resp = render_template("watch_closed.html", reason=reason)
    return resp, status


def _check(token):
    """(recording, None) if the link works, else (None-or-recording, reason)."""
    recording_service.ensure_schema()
    rec = Recording.query.filter_by(share_token=token).first() if token else None
    if rec is None:
        return None, "invalid"
    if not rec.share_enabled:
        return rec, "off"
    if rec.finalized and (rec.is_expired or rec.deleted_at):
        return rec, "expired"
    if not rec.finalized:
        return rec, "pending"
    return rec, None


@watch_bp.route("/watch/<token>")
def watch(token):
    rec, problem = _check(token)
    if problem == "invalid":
        return _closed("invalid", 404)
    if problem:
        return _closed(problem)
    meeting = rec.meeting
    return render_template(
        "watch.html", meeting=meeting, recording=rec,
        video_url=url_for("watch.video", token=token),
    )


@watch_bp.route("/watch/<token>/video")
def video(token):
    rec, problem = _check(token)
    if problem:
        abort(404)
    return recording_service.open_recording_response(rec, current_app.config["RECORDINGS_BUCKET"])


@watch_bp.route("/recordings/<int:meeting_id>/share", methods=["POST"])
@login_required
def share_settings(meeting_id):
    """Host/admin: switch the watch link on or off, or replace it with a new one."""
    meeting = Meeting.query.get_or_404(meeting_id)
    if not (current_user.is_admin or meeting.is_hosted_by(current_user)):
        abort(403)
    rec = meeting.recording
    if rec is None:
        abort(404)
    action = request.form.get("action")
    if action == "off":
        rec.share_enabled = False
        flash("The watch link is switched off. Nobody can open it until you switch it back on.", "info")
    elif action == "on":
        rec.share_enabled = True
        rec.ensure_share_token()
        flash("The watch link is on again.", "success")
    elif action == "regenerate":
        from models import generate_guest_token
        rec.share_token = generate_guest_token()
        rec.share_enabled = True
        flash("New watch link made. The old one no longer works, so send the new one.", "success")
    else:
        abort(400)
    db.session.commit()
    return redirect(request.referrer or url_for("main.dashboard"))
