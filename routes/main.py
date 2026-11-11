from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user

from models import Meeting, MeetingInvite, MeetingRequest
from school import visible_announcements

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return render_template("landing.html")


@main_bp.route("/dashboard")
@login_required
def dashboard():
    """Admin: the Director's control room. Everyone else: only the sessions
    the admin has invited them to, and the status of their own requests."""
    if current_user.is_admin:
        upcoming = Meeting.query.filter(
            Meeting.host_id == current_user.id, Meeting.status.in_(["scheduled", "live"])
        ).all()
        upcoming.sort(key=lambda m: m.scheduled_time or m.created_at)
        pending_count = MeetingRequest.query.filter_by(status="pending").count()
        return render_template("dashboard.html", upcoming=upcoming, pending_count=pending_count,
                               past=[], my_requests=[], announcements=visible_announcements(current_user, limit=3))

    invited = (
        Meeting.query.join(MeetingInvite, MeetingInvite.meeting_id == Meeting.id)
        .filter(MeetingInvite.user_id == current_user.id)
    )
    upcoming = invited.filter(Meeting.status.in_(["scheduled", "live"])).all()
    upcoming.sort(key=lambda m: m.scheduled_time or m.created_at)
    past = invited.filter(Meeting.status == "ended").order_by(Meeting.ended_at.desc()).limit(10).all()
    my_requests = (
        MeetingRequest.query.filter_by(requester_id=current_user.id)
        .order_by(MeetingRequest.created_at.desc()).limit(5).all()
    )
    return render_template("dashboard.html", upcoming=upcoming, past=past, my_requests=my_requests,
                           pending_count=0, announcements=visible_announcements(current_user, limit=3))
