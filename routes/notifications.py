"""Live notifications. The browser polls /notifications/feed every half
minute; everything here is derived from data that already exists (meetings,
invites, placements, requests), so there is no new table to migrate.

The ones that matter most are about meetings: a session that is about to
start, one that is overdue, and one that has already begun while the person
hasn't joined — i.e. they'd otherwise not know it was happening."""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, url_for
from flask_login import login_required, current_user

from models import (
    Meeting, MeetingInvite, MeetingParticipant, MeetingRequest, StudentTeacherAssignment,
)

notifications_bp = Blueprint("notifications", __name__)

SOON_MINUTES = 15        # "starting soon" window
OVERDUE_MINUTES = 90     # keep nagging about a late session this long
RECENT_DAYS = 3          # how long "you were placed" / "request decided" stay


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _minutes_text(minutes):
    if minutes <= 0:
        return "any moment now"
    return f"in {minutes} minute{'s' if minutes != 1 else ''}"


def build_notifications(user, now=None):
    now = now or datetime.utcnow()
    items = []

    # ---- Meetings -----------------------------------------------------
    # Sessions this person hosts (the admin's own, or a teacher's lessons) that
    # haven't started, plus sessions they're invited to.
    meetings = []
    seen = set()
    if user.can_host:
        for m in Meeting.query.filter(Meeting.host_id == user.id, Meeting.status == "scheduled").all():
            meetings.append(m)
            seen.add(m.id)
    for m in (
        Meeting.query.join(MeetingInvite, MeetingInvite.meeting_id == Meeting.id)
        .filter(MeetingInvite.user_id == user.id, Meeting.status.in_(["scheduled", "live"]))
        .all()
    ):
        if m.id not in seen:
            meetings.append(m)

    joined_ids = set()
    guest_meetings = [m for m in meetings if m.host_id != user.id]
    if guest_meetings:
        joined_ids = {
            mp.meeting_id for mp in MeetingParticipant.query.filter(
                MeetingParticipant.user_id == user.id,
                MeetingParticipant.meeting_id.in_([m.id for m in guest_meetings]),
            ).all()
        }

    for m in meetings:
        room = url_for("meetings.room", room_code=m.room_code)
        label = m.type_label()
        is_host = m.host_id == user.id

        if m.status == "live":
            # Already begun. Only worth interrupting someone who hasn't joined.
            if m.id in joined_ids:
                continue
            items.append({
                "id": f"meeting-{m.id}-live", "kind": "meeting_live", "level": "urgent",
                "title": f"{m.title} has started",
                "body": f"This {label.lower()} is already under way and you haven't joined yet.",
                "url": room, "action": "Join now",
                "at": _iso(m.call_started_at or m.scheduled_time),
            })
            continue

        if not m.scheduled_time:
            continue
        delta = m.scheduled_time - now
        minutes = int(delta.total_seconds() // 60)  # negative once overdue

        if timedelta(0) <= delta <= timedelta(minutes=SOON_MINUTES):
            mins_left = -(-int(delta.total_seconds()) // 60)  # round up
            body = f"Your {label.lower()} starts {_minutes_text(mins_left)}."
            items.append({
                "id": f"meeting-{m.id}-soon", "kind": "meeting_soon", "level": "warn",
                "title": f"{m.title} is about to start", "body": body,
                "url": room, "action": "Start session" if is_host else "Open room",
                "at": _iso(m.scheduled_time),
            })
        elif -timedelta(minutes=OVERDUE_MINUTES) <= delta < timedelta(0):
            late = abs(minutes)
            if is_host:
                body = f"Scheduled {late} minute{'s' if late != 1 else ''} ago and not started. Your guests are waiting."
                action = "Start session"
            else:
                body = f"Was due {late} minute{'s' if late != 1 else ''} ago. The host hasn't opened the room yet."
                action = "Open room"
            items.append({
                "id": f"meeting-{m.id}-overdue", "kind": "meeting_overdue", "level": "urgent",
                "title": f"{m.title} should have started", "body": body,
                "url": room, "action": action, "at": _iso(m.scheduled_time),
            })

    # ---- Placements (students and teachers) ---------------------------
    cutoff = now - timedelta(days=RECENT_DAYS)
    if not user.is_admin:
        for a in StudentTeacherAssignment.query.filter(
            StudentTeacherAssignment.created_at >= cutoff,
            (StudentTeacherAssignment.student_id == user.id) | (StudentTeacherAssignment.teacher_id == user.id),
        ).all():
            if a.student_id == user.id:
                title, body = "You've been placed with a teacher", f"{a.teacher.name} is now your teacher."
            else:
                title, body = "A new student was placed with you", f"{a.student.name} has been placed in your class."
            items.append({
                "id": f"placement-{a.id}", "kind": "placement", "level": "info",
                "title": title, "body": body, "url": url_for("portal.index"),
                "action": "View", "at": _iso(a.created_at),
            })

        # ---- Decisions on the person's own requests ---------------------
        for r in MeetingRequest.query.filter(
            MeetingRequest.requester_id == user.id,
            MeetingRequest.status.in_(["approved", "declined"]),
            MeetingRequest.decided_at >= cutoff,
        ).all():
            ok = r.status == "approved"
            items.append({
                "id": f"request-{r.id}-{r.status}", "kind": "request", "level": "info",
                "title": "Your request was approved" if ok else "Your request was declined",
                "body": r.topic, "url": url_for("main.dashboard"),
                "action": "View", "at": _iso(r.decided_at),
            })
    else:
        pending = MeetingRequest.query.filter_by(status="pending").count()
        if pending:
            items.append({
                # Changes with the count, so a new request re-notifies.
                "id": f"admin-requests-{pending}", "kind": "requests_waiting", "level": "info",
                "title": f"{pending} request{'s' if pending != 1 else ''} waiting for you",
                "body": "People are waiting for a decision on their consultation or meeting request.",
                "url": url_for("admin.requests_queue"), "action": "Review", "at": None,
            })

    order = {"urgent": 0, "warn": 1, "info": 2}
    items.sort(key=lambda i: (order[i["level"]], i["at"] or ""))
    return items


@notifications_bp.route("/notifications/feed")
@login_required
def feed():
    resp = jsonify({"items": build_notifications(current_user)})
    resp.headers["Cache-Control"] = "no-store"
    return resp
