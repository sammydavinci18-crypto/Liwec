"""Shared read-side helpers for the school portal: who can see what, and the
summaries the portal, dashboard and timetable are built from. Kept out of the
route modules so the dashboard and the portal use exactly the same rules."""
from datetime import datetime, timedelta

from extensions import db
from models import (
    Announcement, Meeting, MeetingInvite, ParentStudentLink, ProgressReport,
    SchoolClass, StudentProfile, StudentTeacherAssignment, User,
    MAX_SESSION_MINUTES,
)

DEFAULT_CLASSES = ["Year 6", "Year 7", "Year 8", "Year 9", "Year 10", "Year 11",
                   "Year 12", "Year 13", "Bridging"]


def seed_default_classes():
    """First run only: give the admin a starting list of classes to rename,
    archive or add to. Classes are never deleted, so this can't re-run later."""
    if SchoolClass.query.first():
        return
    for i, name in enumerate(DEFAULT_CLASSES):
        db.session.add(SchoolClass(name=name, sort_order=i))
    db.session.commit()


def active_classes():
    return (SchoolClass.query.filter_by(is_active=True)
            .order_by(SchoolClass.sort_order, SchoolClass.name).all())


# ---------------------------------------------------------------------------
# Class rosters: who is on a timetabled lesson
# ---------------------------------------------------------------------------

def class_roster(teacher_id, class_id):
    """Enrolled, not-banned students who are in `class_id` AND placed with this
    teacher. This is the whole class-separation rule: placing Year 7 and
    Year 8 students with the same physics teacher still gives two rosters."""
    return (
        User.query
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .join(StudentTeacherAssignment, StudentTeacherAssignment.student_id == User.id)
        .filter(
            StudentTeacherAssignment.teacher_id == teacher_id,
            StudentProfile.class_id == class_id,
            StudentProfile.status == "approved",
            User.is_banned.is_(False),
        )
        .order_by(User.name).all()
    )


def sync_lesson_roster(meeting):
    """Make a timetabled lesson's roster-sourced invites match the current
    class list. Hand-added invites are never touched. Caller commits."""
    if not meeting.class_id:
        return
    wanted = {u.id for u in class_roster(meeting.host_id, meeting.class_id)}
    wanted.discard(meeting.host_id)
    roster_rows = {i.user_id: i for i in meeting.invites if i.source == "roster"}
    manual = {i.user_id for i in meeting.invites if i.source != "roster"}
    for uid, invite in roster_rows.items():
        if uid not in wanted:
            db.session.delete(invite)
    for uid in wanted - set(roster_rows) - manual:
        db.session.add(MeetingInvite(meeting_id=meeting.id, user_id=uid, source="roster"))


def resync_rosters(teacher_id=None, class_id=None):
    """Call after a placement is made/removed or a student changes class: every
    upcoming lesson for that teacher / class gets its roster refreshed.
    Finished lessons keep their guest list so history and recordings stay put.
    Caller commits."""
    q = Meeting.query.filter(Meeting.class_id.isnot(None), Meeting.status.in_(["scheduled", "live"]))
    if teacher_id is not None:
        q = q.filter(Meeting.host_id == teacher_id)
    if class_id is not None:
        q = q.filter(Meeting.class_id == class_id)
    for m in q.all():
        sync_lesson_roster(m)


def find_conflict(start, minutes, host_id=None, class_id=None, ignore_id=None):
    """Is the teacher (host) or the class already booked in [start, start+minutes)?
    Returns (meeting, "teacher" | "class") or None."""
    end = start + timedelta(minutes=minutes)
    conds = []
    if host_id is not None:
        conds.append(Meeting.host_id == host_id)
    if class_id is not None:
        conds.append(Meeting.class_id == class_id)
    if not conds:
        return None
    candidates = Meeting.query.filter(
        Meeting.status.in_(["scheduled", "live"]),
        Meeting.scheduled_time.isnot(None),
        Meeting.scheduled_time < end,
        Meeting.scheduled_time > start - timedelta(minutes=MAX_SESSION_MINUTES),
        db.or_(*conds),
    ).all()
    for m in candidates:
        if m.id == ignore_id or m.ends_at() <= start:
            continue
        return m, ("teacher" if host_id is not None and m.host_id == host_id else "class")
    return None


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
        have = {m.id for m in meetings}
        meetings += [m for m in Meeting.query.filter(
            Meeting.host_id == user_id, Meeting.status.in_(["scheduled", "live"])
        ).all() if m.id not in have]
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
