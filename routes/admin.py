import secrets
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, redirect, url_for, request, flash, abort, current_app,
    send_from_directory,
)
from flask_login import login_required, current_user
from sqlalchemy import func
from werkzeug.datastructures import MultiDict

import storage
from extensions import db
from models import User, Meeting, MeetingInvite, MeetingRequest, MEETING_TYPES, SchoolClass
from models import MeetingParticipant
from models import DEFAULT_SESSION_MINUTES, MIN_SESSION_MINUTES, MAX_SESSION_MINUTES
from models import Announcement, ANNOUNCEMENT_AUDIENCES
from models import TeacherProfile, TeacherVerificationDocument, CAMBRIDGE_STAGES, STUDENT_PROGRAMS
from models import StudentProfile, StudentVerificationDocument, StudentTeacherAssignment, ParentStudentLink
from permissions import admin_required
from school import (
    active_classes, class_roster, find_conflict, resync_rosters, sync_lesson_roster,
)

admin_bp = Blueprint("admin", __name__)


# ---------------------------------------------------------------------------
# Sessions. The admin hosts consultations and ordinary meetings. Teaching is
# different: a timetabled lesson (one class + one teacher) or an ad-hoc session
# the admin sets up for a teacher and chosen people. In those the TEACHER is
# the host — they open the room and admit the guests — and the admin is not
# part of it. In every case: no invite, no entry.
# ---------------------------------------------------------------------------

def _parse_local_datetime(raw):
    try:
        return datetime.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _parse_minutes(raw):
    """Session length in minutes: blank -> the default, bad/out of range -> None."""
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_SESSION_MINUTES
    try:
        minutes = int(raw)
    except ValueError:
        return None
    return minutes if MIN_SESSION_MINUTES <= minutes <= MAX_SESSION_MINUTES else None


_LENGTH_ERROR = f"Session length must be between {MIN_SESSION_MINUTES} and {MAX_SESSION_MINUTES} minutes."


def _invitable_users(exclude_ids=()):
    return (
        User.query.filter(User.is_banned.is_(False), User.id != current_user.id)
        .filter(~User.id.in_(list(exclude_ids) or [0]))
        .order_by(User.name).all()
    )


def _approved_teachers():
    return (
        User.query.join(TeacherProfile, TeacherProfile.user_id == User.id)
        .filter(TeacherProfile.status == "approved", User.is_banned.is_(False))
        .order_by(User.name).all()
    )


def _conflict_message(found):
    other, who = found
    what = "That teacher is" if who == "teacher" else "That class is"
    return (f"{what} already booked at that time: \"{other.title}\" on "
            f"{other.scheduled_time:%a %d %b %H:%M} UTC. Pick another time.")


@admin_bp.route("/admin/meetings")
@login_required
@admin_required
def meetings():
    active = Meeting.query.filter(Meeting.status.in_(["scheduled", "live"])).all()
    active.sort(key=lambda m: m.scheduled_time or m.created_at)
    past = (
        Meeting.query.filter(Meeting.status.in_(["ended", "cancelled"]))
        .order_by(Meeting.created_at.desc()).limit(30).all()
    )
    return render_template("admin/meetings.html", active=active, past=past)


def _render_new_meeting(form):
    return render_template("admin/new_meeting.html", users=_invitable_users(),
                           teachers=_approved_teachers(), classes=active_classes(),
                           types=MEETING_TYPES, form=form)


@admin_bp.route("/admin/meetings/new", methods=["GET", "POST"])
@login_required
@admin_required
def new_meeting():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        host_mode = "teacher" if request.form.get("host_mode") == "teacher" else "admin"
        meeting_type = request.form.get("meeting_type", "consultation")
        if meeting_type not in {k for k, _ in MEETING_TYPES}:
            meeting_type = "consultation"
        scheduled_time = _parse_local_datetime(request.form.get("scheduled_time"))
        minutes = _parse_minutes(request.form.get("duration_minutes"))
        description = request.form.get("description", "").strip()

        if not title or scheduled_time is None:
            flash("A title and a date/time are required.", "error")
            return _render_new_meeting(request.form)
        if minutes is None:
            flash(_LENGTH_ERROR, "error")
            return _render_new_meeting(request.form)

        host = current_user
        if host_mode == "teacher":
            teacher_id = request.form.get("host_teacher_id", type=int)
            host = User.query.get(teacher_id) if teacher_id else None
            if not host or host.is_banned or not host.is_approved_teacher:
                flash("Pick an approved teacher to host this session.", "error")
                return _render_new_meeting(request.form)

        guest_ids = {int(x) for x in request.form.getlist("guests") if x.isdigit()}

        if host_mode == "teacher":
            # Shortcut: add the teacher's placed students — all of them, or just
            # one class's worth.
            bulk = request.form.get("bulk_students", "")
            if bulk == "all":
                guest_ids |= {a.student_id for a in
                              StudentTeacherAssignment.query.filter_by(teacher_id=host.id).all()}
            elif bulk.isdigit():
                guest_ids |= {u.id for u in class_roster(host.id, int(bulk))}
        else:
            # Class shortcut (admin-hosted): invite a teacher together with every
            # student the admin has placed with them.
            teacher_id = request.form.get("class_teacher_id", type=int)
            if teacher_id:
                if TeacherProfile.query.filter_by(user_id=teacher_id, status="approved").first():
                    guest_ids.add(teacher_id)
                    for a in StudentTeacherAssignment.query.filter_by(teacher_id=teacher_id).all():
                        guest_ids.add(a.student_id)

        valid_ids = {u.id for u in User.query.filter(User.id.in_(list(guest_ids) or [0]),
                                                      User.is_banned.is_(False)).all()}
        valid_ids.discard(current_user.id)
        valid_ids.discard(host.id)

        if host_mode == "teacher":
            if not valid_ids:
                flash("Pick at least one student or guest for the teacher to meet.", "error")
                return _render_new_meeting(request.form)
            found = find_conflict(scheduled_time, minutes, host_id=host.id)
            if found:
                flash(_conflict_message(found), "error")
                return _render_new_meeting(request.form)

        meeting = Meeting(
            title=title[:200], host_id=host.id, scheduled_time=scheduled_time,
            meeting_type=meeting_type, description=description, status="scheduled",
            duration_minutes=minutes,
        )
        db.session.add(meeting)
        db.session.flush()
        for uid in valid_ids:
            db.session.add(MeetingInvite(meeting_id=meeting.id, user_id=uid, source="manual"))
        db.session.commit()
        if host_mode == "teacher":
            flash(f"Session scheduled. {host.name} will host it for {len(valid_ids)} guest(s); "
                  f"you are not part of it.", "success")
        else:
            flash(f"Session scheduled with {len(valid_ids)} guest(s).", "success")
        return redirect(url_for("admin.meeting_detail", meeting_id=meeting.id))

    return _render_new_meeting({})


@admin_bp.route("/admin/meetings/<int:meeting_id>")
@login_required
@admin_required
def meeting_detail(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    invited_ids = [i.user_id for i in meeting.invites] + [meeting.host_id]
    attendance = (
        MeetingParticipant.query.filter_by(meeting_id=meeting.id)
        .order_by(MeetingParticipant.joined_at).all()
    )
    came = {r.user_id for r in attendance}
    # Invited but never joined (only meaningful once the session has started).
    absent = [i.user for i in meeting.invites if i.user_id not in came] \
        if meeting.status in ("live", "ended") else []
    return render_template(
        "admin/meeting_detail.html", meeting=meeting,
        invitable=_invitable_users(exclude_ids=invited_ids),
        attendance=attendance, absent=absent,
    )


@admin_bp.route("/admin/meetings/<int:meeting_id>/invite", methods=["POST"])
@login_required
@admin_required
def meeting_invite(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    if meeting.status in ("ended", "cancelled"):
        flash("This session is over — you can't change its guest list.", "error")
        return redirect(url_for("admin.meeting_detail", meeting_id=meeting_id))
    user_id = request.form.get("user_id", type=int)
    user = User.query.get(user_id) if user_id else None
    if not user or user.is_banned or user.id == current_user.id:
        flash("Pick a valid user to invite.", "error")
    elif user.id == meeting.host_id:
        flash(f"{user.name} is the host of this session.", "info")
    elif meeting.is_invited(user):
        flash(f"{user.name} is already invited.", "info")
    else:
        db.session.add(MeetingInvite(meeting_id=meeting.id, user_id=user.id, source="manual"))
        db.session.commit()
        flash(f"{user.name} invited.", "success")
    return redirect(url_for("admin.meeting_detail", meeting_id=meeting_id))


@admin_bp.route("/admin/meetings/<int:meeting_id>/invite/<int:user_id>/remove", methods=["POST"])
@login_required
@admin_required
def meeting_uninvite(meeting_id, user_id):
    invite = MeetingInvite.query.filter_by(meeting_id=meeting_id, user_id=user_id).first_or_404()
    if invite.source == "roster" and invite.meeting.status in ("scheduled", "live"):
        # The class list is the single source of truth for a lesson's roster;
        # removing the invite here would just be undone at the next sync.
        flash("This student is on the class list for this lesson. Change their class or "
              "remove their placement with this teacher instead.", "error")
        return redirect(url_for("admin.meeting_detail", meeting_id=meeting_id))
    db.session.delete(invite)
    db.session.commit()
    flash("Guest removed from the invite list.", "info")
    return redirect(url_for("admin.meeting_detail", meeting_id=meeting_id))


@admin_bp.route("/admin/meetings/<int:meeting_id>/reschedule", methods=["POST"])
@login_required
@admin_required
def meeting_reschedule(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    new_time = _parse_local_datetime(request.form.get("scheduled_time"))
    minutes = _parse_minutes(request.form.get("duration_minutes")) \
        if request.form.get("duration_minutes") else meeting.length_minutes()
    if meeting.status != "scheduled":
        flash("Only a session that hasn't started can be rescheduled.", "error")
    elif new_time is None:
        flash("Enter a valid date and time.", "error")
    elif minutes is None:
        flash(_LENGTH_ERROR, "error")
    else:
        found = find_conflict(
            new_time, minutes,
            host_id=meeting.host_id if meeting.led_by_teacher() else None,
            class_id=meeting.class_id, ignore_id=meeting.id,
        )
        if found:
            flash(_conflict_message(found), "error")
        else:
            meeting.scheduled_time = new_time
            meeting.duration_minutes = minutes
            db.session.commit()
            flash("Session rescheduled.", "success")
    return redirect(url_for("admin.meeting_detail", meeting_id=meeting_id))


@admin_bp.route("/admin/meetings/<int:meeting_id>/cancel", methods=["POST"])
@login_required
@admin_required
def meeting_cancel(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    if meeting.status != "scheduled":
        flash("Only a session that hasn't started can be cancelled — the host ends a live one from inside its room.", "error")
    else:
        meeting.status = "cancelled"
        db.session.commit()
        flash("Session cancelled. Guests can no longer enter it.", "info")
    return redirect(url_for("admin.meeting_detail", meeting_id=meeting_id))


@admin_bp.route("/admin/meetings/<int:meeting_id>/cancel-series", methods=["POST"])
@login_required
@admin_required
def meeting_cancel_series(meeting_id):
    """Cancel this lesson and every later, not-yet-started one in its weekly series."""
    meeting = Meeting.query.get_or_404(meeting_id)
    if not meeting.series_id or not meeting.scheduled_time:
        flash("This session isn't part of a repeating series.", "error")
        return redirect(url_for("admin.meeting_detail", meeting_id=meeting_id))
    later = Meeting.query.filter(
        Meeting.series_id == meeting.series_id, Meeting.status == "scheduled",
        Meeting.scheduled_time >= meeting.scheduled_time,
    ).all()
    for m in later:
        m.status = "cancelled"
    db.session.commit()
    flash(f"Cancelled {len(later)} lesson(s) in this series.", "info")
    return redirect(url_for("admin.meeting_detail", meeting_id=meeting_id))


# ---------------------------------------------------------------------------
# Classes (year groups) — each student is in one. A timetabled lesson is for
# one class, so Year 7 and Year 8 can share a teacher and a subject and still
# have separate lessons, and Bridging students are their own group.
# ---------------------------------------------------------------------------

def _class_name_taken(name, ignore_id=None):
    q = SchoolClass.query.filter(func.lower(SchoolClass.name) == name.lower())
    if ignore_id:
        q = q.filter(SchoolClass.id != ignore_id)
    return q.first() is not None


@admin_bp.route("/admin/classes")
@login_required
@admin_required
def classes():
    all_classes = SchoolClass.query.order_by(SchoolClass.sort_order, SchoolClass.name).all()
    student_counts = dict(
        db.session.query(StudentProfile.class_id, func.count(StudentProfile.id))
        .filter(StudentProfile.status == "approved", StudentProfile.class_id.isnot(None))
        .group_by(StudentProfile.class_id).all()
    )
    lesson_counts = dict(
        db.session.query(Meeting.class_id, func.count(Meeting.id))
        .filter(Meeting.class_id.isnot(None), Meeting.status == "scheduled")
        .group_by(Meeting.class_id).all()
    )
    return render_template("admin/classes.html", classes=all_classes,
                           student_counts=student_counts, lesson_counts=lesson_counts)


@admin_bp.route("/admin/classes/create", methods=["POST"])
@login_required
@admin_required
def class_create():
    name = request.form.get("name", "").strip()[:60]
    if not name:
        flash("Give the class a name, e.g. \"Year 8\" or \"Bridging\".", "error")
    elif _class_name_taken(name):
        flash(f"There is already a class called \"{name}\".", "error")
    else:
        last = db.session.query(func.max(SchoolClass.sort_order)).scalar()
        db.session.add(SchoolClass(name=name, sort_order=(last or 0) + 1))
        db.session.commit()
        flash(f"Class \"{name}\" added.", "success")
    return redirect(url_for("admin.classes"))


@admin_bp.route("/admin/classes/<int:class_id>/rename", methods=["POST"])
@login_required
@admin_required
def class_rename(class_id):
    cls = SchoolClass.query.get_or_404(class_id)
    name = request.form.get("name", "").strip()[:60]
    if not name:
        flash("A class needs a name.", "error")
    elif _class_name_taken(name, ignore_id=cls.id):
        flash(f"There is already a class called \"{name}\".", "error")
    else:
        cls.name = name
        db.session.commit()
        flash("Class renamed.", "success")
    return redirect(url_for("admin.classes"))


@admin_bp.route("/admin/classes/<int:class_id>/toggle", methods=["POST"])
@login_required
@admin_required
def class_toggle(class_id):
    """Archive/restore. Classes are never deleted so past lessons keep their label."""
    cls = SchoolClass.query.get_or_404(class_id)
    cls.is_active = not cls.is_active
    db.session.commit()
    flash(f"\"{cls.name}\" {'restored' if cls.is_active else 'archived'}.", "info")
    return redirect(url_for("admin.classes"))


@admin_bp.route("/admin/students/<int:user_id>/class", methods=["POST"])
@login_required
@admin_required
def set_student_class(user_id):
    profile = StudentProfile.query.filter_by(user_id=user_id).first_or_404()
    new_id = request.form.get("class_id", type=int)
    if new_id and not SchoolClass.query.get(new_id):
        flash("That class doesn't exist.", "error")
        return redirect(url_for("admin.placements"))
    old_id = profile.class_id
    profile.class_id = new_id or None
    db.session.flush()
    # Lessons for the class they left lose them; lessons for the new class gain them.
    for cid in {old_id, profile.class_id} - {None}:
        resync_rosters(class_id=cid)
    db.session.commit()
    flash(f"{profile.user.name} is now in {profile.class_label()}." if profile.class_id
          else f"{profile.user.name} was taken out of their class.", "success")
    return redirect(url_for("admin.placements", class_id=request.form.get("back_class", type=int)))


# ---------------------------------------------------------------------------
# Timetable — the admin prepares lessons: class + teacher + subject + time
# (optionally repeating weekly). They are ordinary sessions, hosted by the
# teacher, whose guest list is exactly the students of THAT class placed with
# THAT teacher. The admin doesn't open, admit or attend.
# ---------------------------------------------------------------------------

MAX_REPEAT_WEEKS = 40


@admin_bp.route("/admin/timetable")
@login_required
@admin_required
def timetable():
    class_id = request.args.get("class_id", type=int)
    teacher_id = request.args.get("teacher_id", type=int)
    q = Meeting.query.filter(Meeting.class_id.isnot(None))
    if class_id:
        q = q.filter(Meeting.class_id == class_id)
    if teacher_id:
        q = q.filter(Meeting.host_id == teacher_id)
    upcoming = q.filter(Meeting.status.in_(["scheduled", "live"])).all()
    upcoming.sort(key=lambda m: m.scheduled_time or m.created_at)
    past = (
        q.filter(Meeting.status.in_(["ended", "cancelled"]))
        .order_by(Meeting.scheduled_time.desc()).limit(30).all()
    )
    return render_template(
        "admin/timetable.html", upcoming=upcoming, past=past,
        classes=SchoolClass.query.order_by(SchoolClass.sort_order, SchoolClass.name).all(),
        teachers=_approved_teachers(), class_id=class_id, teacher_id=teacher_id,
    )


@admin_bp.route("/admin/timetable/new", methods=["GET", "POST"])
@login_required
@admin_required
def timetable_new():
    def render(form):
        return render_template(
            "admin/timetable_new.html", form=form, classes=active_classes(),
            teachers=_approved_teachers(), max_weeks=MAX_REPEAT_WEEKS,
            default_minutes=DEFAULT_SESSION_MINUTES,
        )

    if request.method == "GET":
        return render(request.args)

    form = request.form
    cls = SchoolClass.query.get(form.get("class_id", type=int) or 0)
    teacher = User.query.get(form.get("teacher_id", type=int) or 0)
    subject = form.get("subject", "").strip()[:120]
    start = _parse_local_datetime(form.get("scheduled_time"))
    minutes = _parse_minutes(form.get("duration_minutes"))
    weeks = form.get("repeat_weeks", default=1, type=int) or 1

    if not cls or not cls.is_active:
        flash("Pick a class.", "error")
    elif not teacher or teacher.is_banned or not teacher.is_approved_teacher:
        flash("Pick an approved teacher.", "error")
    elif not subject:
        flash("Enter the subject being taught.", "error")
    elif start is None:
        flash("Enter a valid date and time.", "error")
    elif start < datetime.utcnow() - timedelta(minutes=5):
        flash("That time is in the past. Pick a time in the future.", "error")
    elif minutes is None:
        flash(_LENGTH_ERROR, "error")
    elif not 1 <= weeks <= MAX_REPEAT_WEEKS:
        flash(f"Repeat for between 1 and {MAX_REPEAT_WEEKS} weeks.", "error")
    else:
        times = [start + timedelta(weeks=i) for i in range(weeks)]
        for t in times:
            found = find_conflict(t, minutes, host_id=teacher.id, class_id=cls.id)
            if found:
                flash(_conflict_message(found) + " Nothing was added.", "error")
                return render(form)

        title = form.get("title", "").strip()[:200] or f"{subject} · {cls.name}"
        description = form.get("description", "").strip() or None
        series_id = secrets.token_hex(6) if weeks > 1 else None
        created = []
        for t in times:
            m = Meeting(
                title=title, host_id=teacher.id, scheduled_time=t, meeting_type="class",
                description=description, status="scheduled", class_id=cls.id,
                subject=subject, duration_minutes=minutes, series_id=series_id,
            )
            db.session.add(m)
            created.append(m)
        db.session.flush()
        for m in created:
            sync_lesson_roster(m)
        db.session.commit()

        roster = class_roster(teacher.id, cls.id)
        flash(f"Added {len(created)} lesson(s): {cls.name} · {subject} with {teacher.name}, "
              f"{len(roster)} student(s) on the list.", "success")
        if not roster:
            flash(f"No {cls.name} students are placed with {teacher.name} yet. "
                  f"Place them and they'll be added to these lessons automatically.", "info")
        return redirect(url_for("admin.timetable", class_id=cls.id))
    return render(form)


# ---------------------------------------------------------------------------
# Requests — people ask, the admin decides. Approval creates the session.
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/requests")
@login_required
@admin_required
def requests_queue():
    pending = (
        MeetingRequest.query.filter_by(status="pending")
        .order_by(MeetingRequest.created_at.asc()).all()
    )
    decided = (
        MeetingRequest.query.filter(MeetingRequest.status.in_(["approved", "declined", "cancelled"]))
        .order_by(MeetingRequest.created_at.desc()).limit(30).all()
    )
    return render_template("admin/requests.html", pending=pending, decided=decided)


@admin_bp.route("/admin/requests/<int:request_id>/approve", methods=["POST"])
@login_required
@admin_required
def request_approve(request_id):
    req = MeetingRequest.query.get_or_404(request_id)
    if req.status != "pending":
        flash("That request has already been handled.", "error")
        return redirect(url_for("admin.requests_queue"))

    scheduled_time = _parse_local_datetime(request.form.get("scheduled_time"))
    if scheduled_time is None:
        flash("Set the date and time for the session before approving.", "error")
        return redirect(url_for("admin.requests_queue"))

    title = request.form.get("title", "").strip() or f"{req.kind_label()}: {req.topic}"
    meeting = Meeting(
        title=title[:200], host_id=current_user.id, scheduled_time=scheduled_time,
        meeting_type="meeting" if req.kind == "meeting" else "consultation",
        description=req.details, status="scheduled",
    )
    db.session.add(meeting)
    db.session.flush()
    db.session.add(MeetingInvite(meeting_id=meeting.id, user_id=req.requester_id))

    req.status = "approved"
    req.meeting_id = meeting.id
    req.admin_note = request.form.get("admin_note", "").strip() or None
    req.decided_at = datetime.utcnow()
    req.decided_by_id = current_user.id
    db.session.commit()
    flash(f"Approved. {req.requester.name} can now enter the session at the scheduled time.", "success")
    return redirect(url_for("admin.requests_queue"))


@admin_bp.route("/admin/requests/<int:request_id>/decline", methods=["POST"])
@login_required
@admin_required
def request_decline(request_id):
    req = MeetingRequest.query.get_or_404(request_id)
    if req.status != "pending":
        flash("That request has already been handled.", "error")
        return redirect(url_for("admin.requests_queue"))
    req.status = "declined"
    req.admin_note = request.form.get("admin_note", "").strip() or None
    req.decided_at = datetime.utcnow()
    req.decided_by_id = current_user.id
    db.session.commit()
    flash("Request declined.", "info")
    return redirect(url_for("admin.requests_queue"))


# ---------------------------------------------------------------------------
# Announcements & school calendar — the admin posts; audiences see them.
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/announcements", methods=["GET", "POST"])
@login_required
@admin_required
def announcements():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        audience = request.form.get("audience", "all")
        if audience not in {k for k, _ in ANNOUNCEMENT_AUDIENCES}:
            audience = "all"
        if not title:
            flash("An announcement needs a title.", "error")
        else:
            db.session.add(Announcement(
                title=title[:200],
                body=request.form.get("body", "").strip() or None,
                audience=audience,
                event_date=_parse_local_datetime(request.form.get("event_date")),
                created_by_id=current_user.id,
            ))
            db.session.commit()
            flash("Announcement posted.", "success")
        return redirect(url_for("admin.announcements"))

    items = Announcement.query.order_by(Announcement.created_at.desc()).limit(50).all()
    return render_template("admin/announcements.html", items=items, audiences=ANNOUNCEMENT_AUDIENCES)


@admin_bp.route("/admin/announcements/<int:announcement_id>/delete", methods=["POST"])
@login_required
@admin_required
def announcement_delete(announcement_id):
    item = Announcement.query.get_or_404(announcement_id)
    db.session.delete(item)
    db.session.commit()
    flash("Announcement removed.", "info")
    return redirect(url_for("admin.announcements"))


# ---------------------------------------------------------------------------
# Teacher verification queue — admin-only approval
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/teachers")
@login_required
@admin_required
def teacher_queue():
    pending = (
        TeacherProfile.query.filter_by(status="pending")
        .order_by(TeacherProfile.submitted_at.asc()).all()
    )
    reviewed = (
        TeacherProfile.query.filter(TeacherProfile.status.in_(["approved", "rejected"]))
        .order_by(TeacherProfile.reviewed_at.desc()).limit(30).all()
    )
    return render_template("admin/teacher_queue.html", pending=pending, reviewed=reviewed)


@admin_bp.route("/admin/teachers/<int:profile_id>")
@login_required
@admin_required
def teacher_review(profile_id):
    profile = TeacherProfile.query.get_or_404(profile_id)
    return render_template("admin/teacher_review.html", profile=profile)


@admin_bp.route("/admin/teachers/<int:profile_id>/approve", methods=["POST"])
@login_required
@admin_required
def teacher_approve(profile_id):
    profile = TeacherProfile.query.get_or_404(profile_id)
    profile.status = "approved"
    profile.review_note = None
    profile.reviewed_at = datetime.utcnow()
    profile.reviewed_by_id = current_user.id
    db.session.commit()
    flash(f"{profile.user.name} is now a verified teacher.", "success")
    return redirect(url_for("admin.teacher_queue"))


@admin_bp.route("/admin/teachers/<int:profile_id>/reject", methods=["POST"])
@login_required
@admin_required
def teacher_reject(profile_id):
    profile = TeacherProfile.query.get_or_404(profile_id)
    profile.status = "rejected"
    profile.review_note = request.form.get("review_note", "").strip()
    profile.reviewed_at = datetime.utcnow()
    profile.reviewed_by_id = current_user.id
    db.session.commit()
    flash(f"{profile.user.name}'s application was rejected.", "info")
    return redirect(url_for("admin.teacher_queue"))


@admin_bp.route("/admin/teachers/<int:profile_id>/set-type", methods=["POST"])
@login_required
@admin_required
def teacher_set_type(profile_id):
    profile = TeacherProfile.query.get_or_404(profile_id)
    new_type = request.form.get("teacher_type")
    if new_type in ("official", "independent"):
        profile.teacher_type = new_type
        db.session.commit()
        flash(f"{profile.user.name} is now marked as an {new_type} teacher.", "success")
    return redirect(url_for("admin.teacher_review", profile_id=profile_id))


@admin_bp.route("/admin/teacher-documents/<int:document_id>")
@login_required
def view_teacher_document(document_id):
    doc = TeacherVerificationDocument.query.get_or_404(document_id)
    profile = doc.profile
    if not (current_user.is_admin or profile.user_id == current_user.id):
        abort(403)

    bucket = current_app.config["VERIFICATION_BUCKET"]
    url = storage.signed_url(bucket, doc.stored_filename, expires_in=60)
    if url:
        return redirect(url)

    local_dir = current_app.config["VERIFICATION_DOCS_DIR"]
    return send_from_directory(local_dir, doc.stored_filename, as_attachment=False)


# ---------------------------------------------------------------------------
# Student admission queue — same review flow as teachers
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/students")
@login_required
@admin_required
def student_queue():
    pending = (
        StudentProfile.query.filter_by(status="pending")
        .order_by(StudentProfile.submitted_at.asc()).all()
    )
    reviewed = (
        StudentProfile.query.filter(StudentProfile.status.in_(["approved", "rejected"]))
        .order_by(StudentProfile.reviewed_at.desc()).limit(30).all()
    )
    return render_template("admin/student_queue.html", pending=pending, reviewed=reviewed)


@admin_bp.route("/admin/students/<int:profile_id>")
@login_required
@admin_required
def student_review(profile_id):
    profile = StudentProfile.query.get_or_404(profile_id)
    classes_ = active_classes()
    # A bridging applicant most likely belongs in the Bridging class.
    suggested = profile.class_id
    if not suggested and profile.desired_stage == "bridging":
        suggested = next((c.id for c in classes_ if c.name.lower() == "bridging"), None)
    return render_template("admin/student_review.html", profile=profile,
                           classes=classes_, suggested_class_id=suggested)


@admin_bp.route("/admin/students/<int:profile_id>/approve", methods=["POST"])
@login_required
@admin_required
def student_approve(profile_id):
    profile = StudentProfile.query.get_or_404(profile_id)
    class_id = request.form.get("class_id", type=int)
    if class_id and SchoolClass.query.get(class_id):
        profile.class_id = class_id
    profile.status = "approved"
    profile.review_note = None
    profile.reviewed_at = datetime.utcnow()
    profile.reviewed_by_id = current_user.id
    db.session.commit()
    if profile.class_id:
        flash(f"{profile.user.name} is now an enrolled student in {profile.class_label()}.", "success")
    else:
        flash(f"{profile.user.name} is now an enrolled student. Set their class before placing them.", "success")
    return redirect(url_for("admin.student_queue"))


@admin_bp.route("/admin/students/<int:profile_id>/reject", methods=["POST"])
@login_required
@admin_required
def student_reject(profile_id):
    profile = StudentProfile.query.get_or_404(profile_id)
    profile.status = "rejected"
    profile.review_note = request.form.get("review_note", "").strip()
    profile.reviewed_at = datetime.utcnow()
    profile.reviewed_by_id = current_user.id
    db.session.commit()
    flash(f"{profile.user.name}'s application was rejected.", "info")
    return redirect(url_for("admin.student_queue"))


@admin_bp.route("/admin/student-documents/<int:document_id>")
@login_required
def view_student_document(document_id):
    doc = StudentVerificationDocument.query.get_or_404(document_id)
    profile = doc.profile
    if not (current_user.is_admin or profile.user_id == current_user.id):
        abort(403)

    bucket = current_app.config["VERIFICATION_BUCKET"]
    url = storage.signed_url(bucket, doc.stored_filename, expires_in=60)
    if url:
        return redirect(url)

    local_dir = current_app.config["VERIFICATION_DOCS_DIR"]
    return send_from_directory(local_dir, doc.stored_filename, as_attachment=False)


# ---------------------------------------------------------------------------
# Placements — admin-only. This is the ONLY way a student gets connected to
# a teacher; students can't browse-and-book a teacher for school purposes.
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/placements")
@login_required
@admin_required
def placements():
    all_classes = SchoolClass.query.order_by(SchoolClass.sort_order, SchoolClass.name).all()
    # ?class_id=<id> narrows the student list and placements to one class;
    # -1 means "students with no class yet".
    class_filter = request.args.get("class_id", type=int)

    students_q = (
        StudentProfile.query.filter_by(status="approved")
        .join(User, StudentProfile.user_id == User.id).order_by(User.name)
    )
    if class_filter == -1:
        students_q = students_q.filter(StudentProfile.class_id.is_(None))
    elif class_filter:
        students_q = students_q.filter(StudentProfile.class_id == class_filter)
    approved_students = students_q.all()
    all_approved = StudentProfile.query.filter_by(status="approved").all()

    approved_teachers = (
        TeacherProfile.query.filter_by(status="approved")
        .join(User, TeacherProfile.user_id == User.id).order_by(User.name).all()
    )
    existing = StudentTeacherAssignment.query.order_by(StudentTeacherAssignment.created_at.desc()).all()

    # Who is placed with whom, so the lists can show it at a glance — and, per
    # teacher, split by class: the same teacher's Year 7s and Year 8s are
    # different groups.
    teachers_of = {}   # student user_id -> [teacher User]
    class_names = {c.id: c.name for c in all_classes}
    grouped = {}       # teacher user_id -> {class_id|None: [student User]}
    for a in existing:
        teachers_of.setdefault(a.student_id, []).append(a.teacher)
        sp = a.student.student_profile
        grouped.setdefault(a.teacher_id, {}).setdefault(sp.class_id if sp else None, []).append(a.student)
    teacher_groups = {
        tid: sorted(
            [(class_names.get(cid, "No class yet"), cid, users) for cid, users in by_class.items()],
            key=lambda g: (g[1] is None, g[0]),
        )
        for tid, by_class in grouped.items()
    }

    def in_filter(a):
        sp = a.student.student_profile
        cid = sp.class_id if sp else None
        if class_filter == -1:
            return cid is None
        return not class_filter or cid == class_filter

    return render_template(
        "admin/placements.html",
        students=approved_students, teachers=approved_teachers,
        assignments=[a for a in existing if in_filter(a)],
        teachers_of=teachers_of, teacher_groups=teacher_groups,
        classes=all_classes, class_filter=class_filter,
        unplaced_count=sum(1 for s_ in all_approved if s_.user_id not in teachers_of),
        unclassed_count=sum(1 for s_ in all_approved if s_.class_id is None),
        preselect_student=request.args.get("student", type=int),
        preselect_teacher=request.args.get("teacher", type=int),
    )


@admin_bp.route("/admin/placements/create", methods=["POST"])
@login_required
@admin_required
def create_placement():
    student_id = request.form.get("student_id", type=int)
    teacher_id = request.form.get("teacher_id", type=int)
    notes = request.form.get("notes", "").strip()

    student_profile = StudentProfile.query.filter_by(user_id=student_id, status="approved").first()
    teacher_profile = TeacherProfile.query.filter_by(user_id=teacher_id, status="approved").first()
    if not student_profile or not teacher_profile:
        flash("Pick an approved student and an approved teacher.", "error")
        return redirect(url_for("admin.placements"))
    if not student_profile.class_id:
        flash(f"Set {student_profile.user.name}'s class first — lessons are timetabled per class.", "error")
        return redirect(url_for("admin.placements", class_id=-1))

    existing = StudentTeacherAssignment.query.filter_by(student_id=student_id, teacher_id=teacher_id).first()
    if existing:
        flash("That student is already placed with that teacher.", "info")
        return redirect(url_for("admin.placements"))

    db.session.add(StudentTeacherAssignment(
        student_id=student_id, teacher_id=teacher_id, assigned_by_id=current_user.id, notes=notes,
    ))
    db.session.flush()
    resync_rosters(teacher_id=teacher_id, class_id=student_profile.class_id)
    db.session.commit()
    flash(f"{student_profile.user.name} ({student_profile.class_label()}) placed with "
          f"{teacher_profile.user.name}.", "success")
    return redirect(url_for("admin.placements"))


@admin_bp.route("/admin/placements/class", methods=["POST"])
@login_required
@admin_required
def create_class_placement():
    """Place every enrolled student of one class with one teacher in one go."""
    class_id = request.form.get("class_id", type=int)
    teacher_id = request.form.get("teacher_id", type=int)
    cls = SchoolClass.query.get(class_id) if class_id else None
    teacher_profile = TeacherProfile.query.filter_by(user_id=teacher_id, status="approved").first()
    if not cls or not teacher_profile:
        flash("Pick a class and an approved teacher.", "error")
        return redirect(url_for("admin.placements"))

    already = {a.student_id for a in StudentTeacherAssignment.query.filter_by(teacher_id=teacher_id).all()}
    added = 0
    for sp in StudentProfile.query.filter_by(class_id=cls.id, status="approved").all():
        if sp.user_id in already or sp.user.is_banned:
            continue
        db.session.add(StudentTeacherAssignment(
            student_id=sp.user_id, teacher_id=teacher_id, assigned_by_id=current_user.id,
            notes=f"Placed with the whole {cls.name} class.",
        ))
        added += 1
    db.session.flush()
    resync_rosters(teacher_id=teacher_id, class_id=cls.id)
    db.session.commit()
    if added:
        flash(f"{added} {cls.name} student(s) placed with {teacher_profile.user.name}.", "success")
    else:
        flash(f"Nobody new to place: every {cls.name} student is already with "
              f"{teacher_profile.user.name}, or the class is empty.", "info")
    return redirect(url_for("admin.placements", class_id=cls.id))


@admin_bp.route("/admin/placements/<int:assignment_id>/remove", methods=["POST"])
@login_required
@admin_required
def remove_placement(assignment_id):
    assignment = StudentTeacherAssignment.query.get_or_404(assignment_id)
    teacher_id = assignment.teacher_id
    db.session.delete(assignment)
    db.session.flush()
    resync_rosters(teacher_id=teacher_id)
    db.session.commit()
    flash("Placement removed. They are off that teacher's upcoming lessons.", "info")
    return redirect(url_for("admin.placements"))


# ---------------------------------------------------------------------------
# User management / moderation
# ---------------------------------------------------------------------------

# Categories the All-users page can be filtered by. A person can belong to
# several at once (a teacher who is also a parent), so these are "has this
# role/relationship" tests, not mutually exclusive buckets.
USER_CATEGORIES = [
    ("all", "All"),
    ("teacher", "Teachers"),
    ("student", "Students"),
    ("parent", "Parents"),
    ("consultant", "Consultants"),
    ("staff", "Staff & admins"),
    ("guest", "Guests"),
]


def _category_conditions():
    """One SQL condition per category. "consultant" = someone who has asked
    for, or been invited to, a consultation. "guest" = a regular account that
    is none of the other things (e.g. someone invited to a one-off meeting)."""
    teacher = User.id.in_(db.session.query(TeacherProfile.user_id))
    student = User.id.in_(db.session.query(StudentProfile.user_id))
    parent = User.id.in_(
        db.session.query(ParentStudentLink.parent_id).filter(ParentStudentLink.status == "approved")
    )
    consultant = db.or_(
        User.id.in_(
            db.session.query(MeetingRequest.requester_id)
            .filter(MeetingRequest.kind.in_(["education", "business"]))
        ),
        User.id.in_(
            db.session.query(MeetingInvite.user_id)
            .join(Meeting, Meeting.id == MeetingInvite.meeting_id)
            .filter(Meeting.meeting_type == "consultation")
        ),
    )
    staff = User.role.in_(["admin", "registrar"])
    guest = db.and_(~teacher, ~student, ~parent, ~consultant, ~staff)
    return {
        "teacher": teacher, "student": student, "parent": parent,
        "consultant": consultant, "staff": staff, "guest": guest,
    }


@admin_bp.route("/admin/users")
@login_required
@admin_required
def users():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "all")
    conds = _category_conditions()
    if category not in conds:
        category = "all"

    base = User.query
    if q:
        like = f"%{q}%"
        base = base.filter(db.or_(User.name.ilike(like), User.email.ilike(like)))

    # Count for each tab (respecting the search box), so the tabs show how
    # many people are behind them.
    counts = {"all": base.count()}
    for key, cond in conds.items():
        counts[key] = base.filter(cond).count()

    query = base.filter(conds[category]) if category != "all" else base
    all_users = query.order_by(User.created_at.desc()).limit(200).all()

    # Work out each listed user's categories for the little badges.
    ids = [u.id for u in all_users] or [0]
    tags = {u.id: [] for u in all_users}
    labels = {"teacher": "Teacher", "student": "Student", "parent": "Parent",
              "consultant": "Consultant"}
    for key, label in labels.items():
        for (uid,) in db.session.query(User.id).filter(User.id.in_(ids), conds[key]).all():
            tags[uid].append(label)
    for u in all_users:
        if u.is_admin:
            tags[u.id].insert(0, "Admin")
        elif u.is_registrar:
            tags[u.id].insert(0, "Registrar")
        if not tags[u.id]:
            tags[u.id].append("Guest")

    return render_template(
        "admin/users.html", users=all_users, q=q, category=category,
        categories=USER_CATEGORIES, counts=counts, tags=tags,
    )


@admin_bp.route("/admin/users/<int:user_id>")
@login_required
@admin_required
def user_detail(user_id):
    user = User.query.get_or_404(user_id)
    sessions_invited_count = MeetingInvite.query.filter_by(user_id=user.id).count()
    requests_made_count = MeetingRequest.query.filter_by(requester_id=user.id).count()
    return render_template(
        "admin/user_detail.html",
        user=user,
        sessions_invited_count=sessions_invited_count,
        requests_made_count=requests_made_count,
    )


@admin_bp.route("/admin/users/<int:user_id>/ban", methods=["POST"])
@login_required
@admin_required
def ban_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You can't ban your own account.", "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))
    if user.is_admin:
        flash("Can't ban another admin from here — change their role first.", "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))

    user.is_banned = True
    user.ban_reason = request.form.get("ban_reason", "").strip()
    user.banned_at = datetime.utcnow()
    db.session.flush()
    if user.student_profile and user.student_profile.class_id:
        resync_rosters(class_id=user.student_profile.class_id)   # drop them from class lessons
    db.session.commit()
    flash(f"{user.name}'s account has been banned.", "info")
    return redirect(url_for("admin.user_detail", user_id=user_id))


@admin_bp.route("/admin/users/<int:user_id>/unban", methods=["POST"])
@login_required
@admin_required
def unban_user(user_id):
    user = User.query.get_or_404(user_id)
    user.is_banned = False
    user.ban_reason = None
    user.banned_at = None
    db.session.flush()
    if user.student_profile and user.student_profile.class_id:
        resync_rosters(class_id=user.student_profile.class_id)
    db.session.commit()
    flash(f"{user.name}'s account has been reinstated.", "success")
    return redirect(url_for("admin.user_detail", user_id=user_id))


@admin_bp.route("/admin/users/<int:user_id>/toggle-registrar", methods=["POST"])
@login_required
@admin_required
def toggle_registrar(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You can't change your own role from here.", "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))
    if user.is_admin:
        flash("Can't demote an admin to registrar from here.", "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))

    user.role = "client" if user.is_registrar else "registrar"
    db.session.commit()
    flash(f"{user.name} is now {'a registrar' if user.is_registrar else 'a regular user'}.", "success")
    return redirect(url_for("admin.user_detail", user_id=user_id))


# ---------------------------------------------------------------------------
# Admin-created accounts — the Director can enrol a teacher or student himself,
# even if they never signed up, then hand them their login details later.
# Accounts made here are approved straight away (the admin is vouching for
# them), so no document review is needed.
# ---------------------------------------------------------------------------

_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alikes (0/O, 1/l/I)


def _generate_password(length=10):
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


@admin_bp.route("/admin/users/new", methods=["GET", "POST"])
@login_required
@admin_required
def create_account():
    form = MultiDict()
    if request.method == "POST":
        form = request.form
        name = form.get("name", "").strip()
        email = form.get("email", "").strip().lower()
        account_type = form.get("account_type", "student")
        password = form.get("password", "").strip() or _generate_password()

        def fail(msg):
            flash(msg, "error")
            return render_template("admin/create_account.html", form=form, classes=active_classes(),
                                   stages=CAMBRIDGE_STAGES, programs=STUDENT_PROGRAMS)

        if account_type not in ("teacher", "student"):
            return fail("Choose whether this is a teacher or a student.")
        if not name or not email or "@" not in email:
            return fail("Enter a name and a valid email address.")
        if len(password) < 6:
            return fail("Password should be at least 6 characters.")
        if User.query.filter_by(email=email).first():
            return fail("An account with that email already exists.")

        user = User(name=name, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        now = datetime.utcnow()
        if account_type == "teacher":
            valid_stage_keys = {k for k, _ in CAMBRIDGE_STAGES}
            stages = [x for x in form.getlist("stages") if x in valid_stage_keys]
            teacher_type = form.get("teacher_type", "official")
            if teacher_type not in ("official", "independent"):
                teacher_type = "official"
            db.session.add(TeacherProfile(
                user_id=user.id,
                headline=form.get("headline", "").strip() or None,
                subjects=form.get("subjects", "").strip() or None,
                stages=",".join(stages),
                teacher_type=teacher_type,
                status="approved", reviewed_at=now, reviewed_by_id=current_user.id,
            ))
        else:
            valid_programs = {k for k, _ in STUDENT_PROGRAMS}
            program = form.get("desired_stage")
            class_id = form.get("class_id", type=int)
            profile = StudentProfile(
                user_id=user.id,
                class_id=class_id if class_id and SchoolClass.query.get(class_id) else None,
                desired_stage=program if program in valid_programs else None,
                subjects_of_interest=form.get("subjects", "").strip() or None,
                notes="Account created by the administrator.",
                status="approved", reviewed_at=now, reviewed_by_id=current_user.id,
            )
            db.session.add(profile)
            db.session.flush()
            profile.ensure_reference()

        db.session.commit()
        return render_template(
            "admin/account_credentials.html", person=user, password=password,
            account_type=account_type, just_created=True,
            login_url=url_for("auth.login", _external=True),
        )

    return render_template("admin/create_account.html", form=form, classes=active_classes(),
                           stages=CAMBRIDGE_STAGES, programs=STUDENT_PROGRAMS)


@admin_bp.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@admin_required
def reset_password(user_id):
    """Set a fresh temporary password so the admin can (re)share login details."""
    user = User.query.get_or_404(user_id)
    if user.is_admin and user.id != current_user.id:
        flash("Can't reset another admin's password from here.", "error")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    password = _generate_password()
    user.set_password(password)
    db.session.commit()
    return render_template(
        "admin/account_credentials.html", person=user, password=password,
        account_type=None, just_created=False,
        login_url=url_for("auth.login", _external=True),
    )
