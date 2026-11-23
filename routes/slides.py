"""Presentations (PowerPoint / PDF) shown to the class during a session.

The host uploads a file here, it is turned into one picture per slide (see
slides.py), and the host presents it over the live session (see the slides_*
events in sockets.py). These routes only handle the files:

    POST /room/<code>/slides/upload               host: upload a presentation
    GET  /room/<code>/slides                      host: the session's presentations + status
    POST /room/<code>/slides/<id>/delete          host: remove one
    GET  /room/<code>/slides/<id>/<n>.jpg         slide n (members: only the deck on screen)
    GET  /room/<code>/slides/<id>/<n>-thumb.jpg   small preview of slide n
"""
import os
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, jsonify, request
from flask_login import current_user, login_required

import slides
import storage
from extensions import db, socketio
from models import Meeting, SlideDeck

slides_bp = Blueprint("slides", __name__)

MAX_DECKS_PER_SESSION = 10


def _meeting_or_404(room_code):
    return Meeting.query.filter_by(room_code=room_code).first_or_404()


def _host_meeting(room_code):
    """The meeting, if the current user hosts it and it is still running."""
    meeting = _meeting_or_404(room_code)
    if not meeting.is_hosted_by(current_user):
        abort(403)
    if meeting.status in ("ended", "cancelled"):
        abort(410)
    return meeting


def _may_view(meeting):
    """Anyone who is genuinely in this session: the host, an invited account,
    a student who came in through the share link, or an admin observing a live
    session. Everyone else is treated as if the slides don't exist."""
    if current_user.is_authenticated:
        if meeting.entry_state(current_user) in ("host", "ok"):
            return True
        if current_user.is_admin and meeting.status == "live":
            return True
    from routes.guest import session_guest
    guest = session_guest(meeting)
    return bool(guest and not guest.removed_at)


def _deck_json(deck):
    return {
        "id": deck.id,
        "title": deck.title,
        "status": deck.status,
        "error": deck.error,
        "pages": deck.page_count,
        "v": deck.version,
    }


def _json_error(message, status=400):
    return jsonify(ok=False, error=message), status


@slides_bp.route("/room/<room_code>/slides", methods=["GET"])
@login_required
def list_decks(room_code):
    meeting = _host_meeting(room_code)
    slides.mark_stale_decks(meeting.id)
    import sockets
    decks = SlideDeck.query.filter_by(meeting_id=meeting.id).order_by(SlideDeck.created_at.desc()).all()
    live = sockets.SLIDES.get(meeting.room_code)
    return jsonify(
        ok=True,
        office_ok=slides.office_available(current_app.config.get("SOFFICE_PATH", "")),
        max_mb=current_app.config["SLIDES_MAX_MB"],
        presenting=live["deck_id"] if live else None,
        decks=[_deck_json(d) for d in decks],
    )


@slides_bp.route("/room/<room_code>/slides/upload", methods=["POST"])
@login_required
def upload(room_code):
    meeting = _host_meeting(room_code)
    cfg = current_app.config

    upload_file = request.files.get("file")
    if upload_file is None or not upload_file.filename:
        return _json_error("Choose a PowerPoint or PDF file first.")

    original = os.path.basename(upload_file.filename.replace("\\", "/"))
    ext = Path(original).suffix.lower()
    if ext not in slides.ALLOWED_EXTENSIONS:
        return _json_error("Upload a PowerPoint (.pptx) or a PDF.")
    if ext in slides.OFFICE_EXTENSIONS and not slides.office_available(cfg.get("SOFFICE_PATH", "")):
        return _json_error(
            "This server can't open PowerPoint files directly. In PowerPoint choose "
            "File > Save As > PDF, then upload that PDF instead."
        )

    max_bytes = cfg["SLIDES_MAX_MB"] * 1024 * 1024
    if request.content_length and request.content_length > max_bytes + 64 * 1024:
        return _json_error(f"That file is too big. The limit is {cfg['SLIDES_MAX_MB']} MB.", 413)

    active = SlideDeck.query.filter(
        SlideDeck.meeting_id == meeting.id, SlideDeck.status != "failed"
    ).count()
    if active >= MAX_DECKS_PER_SESSION:
        return _json_error("This session already has 10 presentations. Remove one first.")

    workdir = slides.new_workdir()
    path = workdir / f"deck{ext}"
    try:
        upload_file.save(path)
        size = path.stat().st_size
        if size == 0:
            raise slides.SlideError("That file is empty.")
        if size > max_bytes:
            raise slides.SlideError(f"That file is too big. The limit is {cfg['SLIDES_MAX_MB']} MB.")
        with open(path, "rb") as fh:
            head = fh.read(8)
        if not any(head.startswith(magic) for magic in slides.MAGIC[ext]):
            raise slides.SlideError("That doesn't look like a real " + ("PDF." if ext == ".pdf" else "PowerPoint file."))
    except slides.SlideError as exc:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
        return _json_error(str(exc))
    except Exception:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    deck = SlideDeck(meeting_id=meeting.id, title=Path(original).stem.strip()[:200] or "Presentation")
    db.session.add(deck)
    db.session.commit()

    app = current_app._get_current_object()
    socketio.start_background_task(slides.process_deck, app, deck.id, str(path), ext)
    return jsonify(ok=True, deck=_deck_json(deck))


@slides_bp.route("/room/<room_code>/slides/<int:deck_id>/delete", methods=["POST"])
@login_required
def delete_deck(room_code, deck_id):
    meeting = _host_meeting(room_code)
    deck = SlideDeck.query.filter_by(id=deck_id, meeting_id=meeting.id).first_or_404()

    import sockets
    live = sockets.SLIDES.get(meeting.room_code)
    if live and live["deck_id"] == deck.id:
        sockets.stop_slides(meeting.room_code)  # the class goes back to the session
    # Forget where the host was in it: ids can be reused (SQLite), and a new deck must start at slide 1.
    sockets.SLIDE_POS.pop((meeting.room_code, deck.id), None)
    import classroom_state
    tools = classroom_state.peek(meeting.room_code)
    if tools:
        tools.ink.drop_deck(deck.id)            # and the teacher's drawing on its slides

    slides.delete_deck_files(current_app._get_current_object(), deck)
    db.session.delete(deck)
    db.session.commit()
    return jsonify(ok=True)


def _serve_page(room_code, deck_id, n, thumb):
    meeting = _meeting_or_404(room_code)
    if not _may_view(meeting):
        abort(404)
    deck = SlideDeck.query.filter_by(id=deck_id, meeting_id=meeting.id).first()
    if deck is None or deck.status != "ready" or not (1 <= n <= deck.page_count):
        abort(404)

    # The host can look at any of their decks; everyone else only at the one
    # that is on screen right now.
    if not meeting.is_hosted_by(current_user):
        import sockets
        live = sockets.SLIDES.get(meeting.room_code)
        if not live or live["deck_id"] != deck.id:
            abort(404)

    try:
        data = storage.read_bytes(current_app.config["RECORDINGS_BUCKET"], deck.page_key(n, thumb=thumb))
    except storage.StorageError as exc:
        current_app.logger.error(f"Slide image fetch failed: {exc}")
        abort(502)
    if data is None:
        abort(404)

    resp = Response(data, mimetype="image/jpeg")
    # A deck's pictures never change once made, so browsers may keep them: that
    # is what makes flipping slides instant for everyone.
    resp.headers["Cache-Control"] = "private, max-age=86400, immutable"
    return resp


@slides_bp.route("/room/<room_code>/slides/<int:deck_id>/<int:n>.jpg")
def page_image(room_code, deck_id, n):
    return _serve_page(room_code, deck_id, n, thumb=False)


@slides_bp.route("/room/<room_code>/slides/<int:deck_id>/<int:n>-thumb.jpg")
def page_thumb(room_code, deck_id, n):
    return _serve_page(room_code, deck_id, n, thumb=True)
