"""Pages and data for the server-side recorder (LiveKit Egress).

Egress opens ONE web page in a headless browser, joins the lesson as a hidden
recorder and films that page. This is that page, so the recording looks like
the lesson: the teacher large on the left, everyone else in a grid, and when
the teacher shows slides or the whiteboard those cover the picture (they are
not video, so the media server could not see them otherwise).

There is no login in a headless browser, so every URL carries a signed,
expiring token made when the recording was started (rtc.egress_view_token).

    GET /egress/<room>/<token>/view                  the layout page
    GET /egress/<room>/<token>/state                 slides + whiteboard state (polled)
    GET /egress/<room>/<token>/slide/<deck>/<n>.jpg  the slide picture on screen
"""
from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request

import rtc
import storage
from models import Meeting, SlideDeck

egress_bp = Blueprint("egress", __name__)


def _authorised_meeting(room_code, token):
    if not rtc.egress_view_token_ok(room_code, token):
        abort(404)
    meeting = Meeting.query.filter_by(room_code=room_code).first()
    if meeting is None:
        abort(404)
    return meeting


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@egress_bp.route("/egress/<room_code>/<token>/view")
def view(room_code, token):
    meeting = _authorised_meeting(room_code, token)
    return _no_store(Response(render_template(
        "egress_view.html", meeting=meeting, room_code=room_code, token=token,
    )))


@egress_bp.route("/egress/<room_code>/<token>/state")
def state(room_code, token):
    meeting = _authorised_meeting(room_code, token)
    import sockets

    payload = {"slides": sockets._slides_payload(meeting.room_code), "wb": {"v": 0, "open": False}}
    board = sockets.WHITEBOARD.get(meeting.room_code)
    if board:
        have = request.args.get("wb", type=int)
        payload["wb"] = {"v": board["v"], "open": board["open"]}
        # The (possibly large) list of strokes is only sent when it has changed.
        if have != board["v"]:
            payload["wb"]["ops"] = board["ops"]
    return _no_store(jsonify(payload))


@egress_bp.route("/egress/<room_code>/<token>/slide/<int:deck_id>/<int:n>.jpg")
def slide(room_code, token, deck_id, n):
    meeting = _authorised_meeting(room_code, token)
    import sockets
    live = sockets.SLIDES.get(meeting.room_code)
    if not live or live["deck_id"] != deck_id:
        abort(404)
    deck = SlideDeck.query.filter_by(id=deck_id, meeting_id=meeting.id).first()
    if deck is None or deck.status != "ready" or not (1 <= n <= deck.page_count):
        abort(404)
    try:
        data = storage.read_bytes(current_app.config["RECORDINGS_BUCKET"], deck.page_key(n, thumb=False))
    except storage.StorageError:
        abort(502)
    if data is None:
        abort(404)
    resp = Response(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp
