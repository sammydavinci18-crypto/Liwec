"""Shared read-side helpers for the school portal: who can see what, and the
summaries the portal, dashboard and timetable are built from. Kept out of the
route modules so the dashboard and the portal use exactly the same rules."""
from datetime import datetime, timedelta

from extensions import db
from models import (
    Announcement, Meeting, MeetingInvite, ParentStudentLink, ProgressReport,
    StudentTeacherAssignment, User,
)


def approved_children(user):
    """The student accounts this user is an approved parent/guardian of."""
    links = ParentStudentLink.query.filter_by(parent_id=user.id, status="approved").all()
    return [link.student for link in links]


def audience_keys(user):
    """Which announcement audiences this user belongs to."""
    if user.is_admin:
        return {"all", "students", "teachers", "parents"}
    keys = {"all"}
    if user.student_profile:
        keys.add("students")
    if user.teacher_profile:
        keys.add("teachers")
    if ParentStudentLink.query.filter_by(parent_id=user.id, status="approved").first():
        keys.add("parents")
    return keys


def visible_announcements(user, limit=None):
    q = (
        Announcement.query.filter(Announcement.audience.in_(list(audience_keys(user))))
        .order_by(Announcement.created_at.desc())
    )
    return q.limit(limit).all() if limit else q.all()


def upcoming_events(user, days=90):
    """School-calendar entries (announcements with a date) from today onward."""
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        Announcement.query.filter(
            Announcement.audience.in_(list(audience_keys(user))),
            Announcement.event_date.isnot(None),
            Announcement.event_date >= start,
            Announcement.event_date <= start + timedelta(days=days),
        )
        .order_by(Announcement.event_date.asc()).all()
    )


def upcoming_sessions_for(user_id, include_hosted=False):
    """Sessions (classes, consultations, meetings) this account is invited to
    that haven't finished, soonest first."""
    cutoff = datetime.utcnow() - timedelta(hours=3)
    q = (
        Meeting.query.join(MeetingInvite, MeetingInvite.meeting_id == Meeting.id)
        .filter(MeetingInvite.user_id == user_id, Meeting.status.in_(["scheduled", "live"]))
    )
    meetings = q.all()
    if include_hosted:
        meetings += Meeting.query.filter(
            Meeting.host_id == user_id, Meeting.status.in_(["scheduled", "live"])
        ).all()
    meetings = [m for m in meetings if m.status == "live" or not m.scheduled_time or m.scheduled_time >= cutoff]
    meetings.sort(key=lambda m: m.scheduled_time or m.created_at)
    return meetings


def reports_for(student_id):
    return (
        ProgressReport.query.filter_by(student_id=student_id)
        .order_by(ProgressReport.created_at.desc()).all()
    )


def teacher_has_student(teacher_id, student_id):
    return StudentTeacherAssignment.query.filter_by(
        teacher_id=teacher_id, student_id=student_id
    ).first() is not None


def can_view_student(viewer, student_id):
    """Reports and the student's school record: the student, their approved
    parents/guardians, teachers placed with them, registrars and the admin."""
    if viewer.id == student_id or viewer.is_admin or viewer.is_registrar:
        return True
    if ParentStudentLink.query.filter_by(
        parent_id=viewer.id, student_id=student_id, status="approved"
    ).first():
        return True
    return teacher_has_student(viewer.id, student_id)


def student_summary(student):
    """Everything the portal shows for one student, in one dict."""
    profile = student.student_profile
    if profile and profile.ensure_reference():
        db.session.commit()
    assignments = StudentTeacherAssignment.query.filter_by(student_id=student.id).all()
    return {
        "student": student,
        "profile": profile,
        "stages": profile.tracker_stages(bool(assignments)) if profile else [],
        "teachers": [a.teacher for a in assignments],
        "sessions": upcoming_sessions_for(student.id),
        "reports": reports_for(student.id),
    }
