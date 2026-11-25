"""YouTube search for the teacher's in-lesson video player.

Playing is done entirely in the browsers (YouTube's own embedded player, kept
in step by the video_* socket events in sockets.py). This module only offers
search, through the YouTube Data API, for the host of a live session:

    GET /room/<code>/video/search?q=...

Results are limited to embeddable videos with SafeSearch on strict (this is a
school), cached for ten minutes, and rate-limited per teacher, because the
free Google quota is shared by the whole site (a search costs 100 of 10,000
daily units, so about 100 searches a day).
"""
import html
import time

import requests
from flask import Blueprint, abort, current_app, jsonify, request
from flask_login import current_user, login_required

from models import Meeting

video_bp = Blueprint("video", __name__)

_CACHE = {}          # query -> (expires, results)
_HITS = {}           # user id -> [timestamps]
CACHE_SECONDS = 600
MAX_SEARCHES = 20    # per teacher, per 10 minutes


@video_bp.route("/room/<room_code>/video/search")
@login_required
def search(room_code):
    meeting = Meeting.query.filter_by(room_code=room_code).first_or_404()
    if not meeting.is_hosted_by(current_user) or meeting.status != "live":
        abort(403)
    key = current_app.config["YOUTUBE_API_KEY"]
    if not key:
        return jsonify(enabled=False, results=[])

    q = " ".join((request.args.get("q") or "").split())[:100]
    if len(q) < 2:
        return jsonify(enabled=True, results=[])

    now = time.time()
    hits = [t for t in _HITS.get(current_user.id, []) if now - t < 600]
    if len(hits) >= MAX_SEARCHES:
        return jsonify(enabled=True, results=[], error="Too many searches. Wait a few minutes, or paste a link."), 429
    cached = _CACHE.get(q.lower())
    if cached and cached[0] > now:
        return jsonify(enabled=True, results=cached[1])
    hits.append(now)
    _HITS[current_user.id] = hits

    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part": "snippet", "type": "video", "videoEmbeddable": "true", "safeSearch": "strict",
                    "maxResults": 12, "q": q, "key": key},
            timeout=10,
        )
        data = r.json()
    except (requests.RequestException, ValueError):
        return jsonify(enabled=True, results=[], error="Could not reach YouTube. Paste a link instead."), 502
    if r.status_code != 200:
        reason = ((data.get("error") or {}).get("errors") or [{}])[0].get("reason", "")
        current_app.logger.error("YouTube search failed (%s): %s", r.status_code, reason)
        msg = ("Today's YouTube search allowance is used up. Paste a link instead."
               if reason == "quotaExceeded" else "YouTube search is not working right now. Paste a link instead.")
        return jsonify(enabled=True, results=[], error=msg), 502

    results = []
    for item in data.get("items", []):
        vid = (item.get("id") or {}).get("videoId")
        sn = item.get("snippet") or {}
        if not vid:
            continue
        thumb = ((sn.get("thumbnails") or {}).get("medium") or {}).get("url", "")
        results.append({"id": vid, "title": html.unescape(sn.get("title", ""))[:120],
                        "channel": html.unescape(sn.get("channelTitle", ""))[:60], "thumb": thumb})
    if len(_CACHE) > 200:
        _CACHE.clear()
    _CACHE[q.lower()] = (now + CACHE_SECONDS, results)
    return jsonify(enabled=True, results=results)
