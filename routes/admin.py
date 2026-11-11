import secrets
from datetime import datetime

from flask import (
    Blueprint, render_template, redirect, url_for, request, flash, abort, current_app,
    send_from_directory,
)
from flask_login import login_required, current_user
from werkzeug.datastructures import MultiDict

import storage
from extensions import db
from models import User, Meeting, MeetingInvite, MeetingRequest, MEETING_TYPES
from models import Announcement, ANNOUNCEMENT_AUDIENCES
from models import TeacherProfile, TeacherVerificationDocument, CAMBRIDGE_STAGES, STUDENT_PROGRAMS
from models import StudentProfile, StudentVerificationDocument, StudentTeacherAssignment
from permissions import admin_required

admin_bp = Blueprint("admin", __name__)


# ---------------------------------------------------------------------------
# Sessions — the admin is the ONLY person who can create or host a meeting.
# Everything on the platform that happens live (consultations, classes,
# ordinary meetings) is scheduled here, with a guest list. No invite, no entry.
# ---------------------------------------------------------------------------

def _parse_local_datetime(raw):
    try:
        return datetime.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _invitable_users(exclude_ids=()):
    return (
        User.query.filter(User.is_banned.is_(False), User.id != current_user.id)
        .filter(~User.id.in_(list(exclude_ids) or [0]))
        .order_by(User.name).all()
    )


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


@admin_bp.route("/admin/meetings/new", methods=["GET", "POST"])
@login_required
@admin_required
def new_meeting():
    teachers = (
        TeacherProfile.query.filter_by(status="approved")
        .join(User, TeacherProfile.user_id == User.id).order_by(User.name).all()
    )
    users_ = _invitable_users()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        meeting_type = request.form.get("meeting_type", "consultation")
        if meeting_type not in {k for k, _ in MEETING_TYPES}:
            meeting_type = "consultation"
        scheduled_time = _parse_local_datetime(request.form.get("scheduled_time"))
        description = request.form.get("description", "").strip()

        if not title or scheduled_time is None:
            flash("A title and a date/time are required.", "error")
            return render_template("admin/new_meeting.html", users=users_, teachers=teachers,
                                   types=MEETING_TYPES, form=request.form)

        guest_ids = {int(x) for x in request.form.getlist("guests") if x.isdigit()}

        # Class shortcut: invite a teacher together with every student the
        # admin has placed with them.
        teacher_id = request.form.get("class_teacher_id", type=int)
        if teacher_id:
            if TeacherProfile.query.filter_by(user_id=teacher_id, status="approved").first():
                guest_ids.add(teacher_id)
                for a in StudentTeacherAssignment.query.filter_by(teacher_id=teacher_id).all():
                    guest_ids.add(a.student_id)

        valid_ids = {u.id for u in User.query.filter(User.id.in_(list(guest_ids) or [0]),
                                                      User.is_banned.is_(False)).all()}
        valid_ids.discard(current_user.id)

        meeting = Meeting(
            title=title[:200], host_id=current_user.id, scheduled_time=scheduled_time,
            meeting_type=meeting_type, description=description, status="scheduled",
        )
        db.session.add(meeting)
        db.session.flush()
        for uid in valid_ids:
            db.session.add(MeetingInvite(meeting_id=meeting.id, user_id=uid))
        db.session.commit()
        flash(f"Session scheduled with {len(valid_ids)} guest(s).", "success")
        return redirect(url_for("admin.meeting_detail", meeting_id=meeting.id))

    return render_template("admin/new_meeting.html", users=users_, teachers=teachers,
                           types=MEETING_TYPES, form={})


@admin_bp.route("/admin/meetings/<int:meeting_id>")
@login_required
@admin_required
def meeting_detail(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    invited_ids = [i.user_id for i in meeting.invites]
    return render_template(
        "admin/meeting_detail.html", meeting=meeting,
        invitable=_invitable_users(exclude_ids=invited_ids),
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
    elif meeting.is_invited(user):
        flash(f"{user.name} is already invited.", "info")
    else:
        db.session.add(MeetingInvite(meeting_id=meeting.id, user_id=user.id))
        db.session.commit()
        flash(f"{user.name} invited.", "success")
    return redirect(url_for("admin.meeting_detail", meeting_id=meeting_id))


@admin_bp.route("/admin/meetings/<int:meeting_id>/invite/<int:user_id>/remove", methods=["POST"])
@login_required
@admin_required
def meeting_uninvite(meeting_id, user_id):
    invite = MeetingInvite.query.filter_by(meeting_id=meeting_id, user_id=user_id).first_or_404()
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
    if meeting.status != "scheduled":
        flash("Only a session that hasn't started can be rescheduled.", "error")
    elif new_time is None:
        flash("Enter a valid date and time.", "error")
    else:
        meeting.scheduled_time = new_time
        db.session.commit()
        flash("Session rescheduled.", "success")
    return redirect(url_for("admin.meeting_detail", meeting_id=meeting_id))


@admin_bp.route("/admin/meetings/<int:meeting_id>/cancel", methods=["POST"])
@login_required
@admin_required
def meeting_cancel(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    if meeting.status != "scheduled":
        flash("Only a session that hasn't started can be cancelled — end a live one from inside its room.", "error")
    else:
        meeting.status = "cancelled"
        db.session.commit()
        flash("Session cancelled. Guests can no longer enter it.", "info")
    return redirect(url_for("admin.meeting_detail", meeting_id=meeting_id))


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
    return render_template("admin/student_review.html", profile=profile)


@admin_bp.route("/admin/students/<int:profile_id>/approve", methods=["POST"])
@login_required
@admin_required
def student_approve(profile_id):
    profile = StudentProfile.query.get_or_404(profile_id)
    profile.status = "approved"
    profile.review_note = None
    profile.reviewed_at = datetime.utcnow()
    profile.reviewed_by_id = current_user.id
    db.session.commit()
    flash(f"{profile.user.name} is now an enrolled student.", "success")
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
    approved_students = (
        StudentProfile.query.filter_by(status="approved")
        .join(User, StudentProfile.user_id == User.id).order_by(User.name).all()
    )
    approved_teachers = (
        TeacherProfile.query.filter_by(status="approved")
        .join(User, TeacherProfile.user_id == User.id).order_by(User.name).all()
    )
    existing = StudentTeacherAssignment.query.order_by(StudentTeacherAssignment.created_at.desc()).all()
    return render_template(
        "admin/placements.html",
        students=approved_students, teachers=approved_teachers, assignments=existing,
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

    existing = StudentTeacherAssignment.query.filter_by(student_id=student_id, teacher_id=teacher_id).first()
    if existing:
        flash("That student is already placed with that teacher.", "info")
        return redirect(url_for("admin.placements"))

    db.session.add(StudentTeacherAssignment(
        student_id=student_id, teacher_id=teacher_id, assigned_by_id=current_user.id, notes=notes,
    ))
    db.session.commit()
    flash("Student placed with teacher.", "success")
    return redirect(url_for("admin.placements"))


@admin_bp.route("/admin/placements/<int:assignment_id>/remove", methods=["POST"])
@login_required
@admin_required
def remove_placement(assignment_id):
    assignment = StudentTeacherAssignment.query.get_or_404(assignment_id)
    db.session.delete(assignment)
    db.session.commit()
    flash("Placement removed.", "info")
    return redirect(url_for("admin.placements"))


# ---------------------------------------------------------------------------
# User management / moderation
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/users")
@login_required
@admin_required
def users():
    q = request.args.get("q", "").strip()
    query = User.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.name.ilike(like), User.email.ilike(like)))
    all_users = query.order_by(User.created_at.desc()).limit(200).all()
    return render_template("admin/users.html", users=all_users, q=q)


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
            return render_template("admin/create_account.html", form=form,
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
            profile = StudentProfile(
                user_id=user.id,
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

    return render_template("admin/create_account.html", form=form,
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
