from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, current_app
from flask_login import login_required, current_user

import storage
from extensions import db
from models import TeacherProfile, TeacherVerificationDocument, CAMBRIDGE_STAGES, StudentTeacherAssignment, Meeting

teachers_bp = Blueprint("teachers", __name__)

ALLOWED_DOC_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
MAX_DOCS_PER_APPLICATION = 5


def _allowed_doc(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_DOC_EXTENSIONS


# ---------------------------------------------------------------------------
# Directory — browse approved teachers
# ---------------------------------------------------------------------------

@teachers_bp.route("/teachers")
@login_required
def directory():
    stage = request.args.get("stage", "all")
    query = TeacherProfile.query.filter_by(status="approved")
    if stage != "all":
        query = query.filter(TeacherProfile.stages.ilike(f"%{stage}%"))
    profiles = query.order_by(TeacherProfile.submitted_at.desc()).all()
    return render_template("teachers/directory.html", profiles=profiles, stage=stage, stages=CAMBRIDGE_STAGES)


@teachers_bp.route("/teachers/<int:user_id>")
@login_required
def profile(user_id):
    profile = TeacherProfile.query.filter_by(user_id=user_id).first_or_404()
    if profile.status != "approved" and profile.user_id != current_user.id and not current_user.is_admin:
        abort(404)
    return render_template("teachers/profile.html", profile=profile)


# ---------------------------------------------------------------------------
# Becoming a teacher — application with verification documents
# ---------------------------------------------------------------------------

@teachers_bp.route("/teachers/apply", methods=["GET", "POST"])
@login_required
def apply():
    existing = current_user.teacher_profile

    if request.method == "POST":
        headline = request.form.get("headline", "").strip()
        bio = request.form.get("bio", "").strip()
        subjects = request.form.get("subjects", "").strip()
        stages = request.form.getlist("stages")
        valid_stage_keys = {k for k, _ in CAMBRIDGE_STAGES}
        stages = [s for s in stages if s in valid_stage_keys]

        files = [f for f in request.files.getlist("documents") if f and f.filename]
        if not existing and not files:
            flash("Please attach at least one verification document (teaching certificate, ID, or CV).", "error")
            return render_template("teachers/apply.html", existing=existing, stages=CAMBRIDGE_STAGES)

        for f in files:
            if not _allowed_doc(f.filename):
                flash(f"'{f.filename}' isn't a supported file type (PDF, PNG, or JPG only).", "error")
                return render_template("teachers/apply.html", existing=existing, stages=CAMBRIDGE_STAGES)

        if existing:
            profile = existing
            profile.headline = headline
            profile.bio = bio
            profile.subjects = subjects
            profile.stages = ",".join(stages)
            if profile.status == "rejected":
                profile.status = "pending"
                profile.review_note = None
                profile.submitted_at = datetime.utcnow()
        else:
            profile = TeacherProfile(
                user_id=current_user.id,
                headline=headline,
                bio=bio,
                subjects=subjects,
                stages=",".join(stages),
                status="pending",
            )
            db.session.add(profile)
            db.session.flush()

        for f in files[:MAX_DOCS_PER_APPLICATION]:
            stored_name = storage.new_object_key(f.filename)
            try:
                storage.upload_bytes(
                    current_app.config["VERIFICATION_BUCKET"], stored_name,
                    f.read(), content_type=f.mimetype or "application/octet-stream",
                )
            except storage.StorageError as exc:
                current_app.logger.error(f"Teacher document upload failed: {exc}")
                flash("Couldn't upload that document — please try again.", "error")
                return render_template("teachers/apply.html", existing=existing, stages=CAMBRIDGE_STAGES)
            db.session.add(TeacherVerificationDocument(
                profile_id=profile.id,
                stored_filename=stored_name,
                original_filename=f.filename,
            ))

        db.session.commit()
        flash("Application submitted. An admin will review your documents and let you know.", "success")
        return redirect(url_for("teachers.my_profile"))

    return render_template("teachers/apply.html", existing=existing, stages=CAMBRIDGE_STAGES)


@teachers_bp.route("/teachers/me")
@login_required
def my_profile():
    profile = current_user.teacher_profile
    if not profile:
        return redirect(url_for("teachers.apply"))
    return render_template("teachers/my_profile.html", profile=profile)


@teachers_bp.route("/teachers/me/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    profile = current_user.teacher_profile
    if not profile:
        return redirect(url_for("teachers.apply"))

    if request.method == "POST":
        profile.headline = request.form.get("headline", "").strip()
        profile.bio = request.form.get("bio", "").strip()
        profile.subjects = request.form.get("subjects", "").strip()
        valid_stage_keys = {k for k, _ in CAMBRIDGE_STAGES}
        stages = [s for s in request.form.getlist("stages") if s in valid_stage_keys]
        profile.stages = ",".join(stages)
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("teachers.my_profile"))

    return render_template("teachers/edit_profile.html", profile=profile, stages=CAMBRIDGE_STAGES)


@teachers_bp.route("/teachers/me/students")
@login_required
def my_students():
    profile = current_user.teacher_profile
    if not profile:
        return redirect(url_for("teachers.apply"))

    assignments = StudentTeacherAssignment.query.filter_by(teacher_id=current_user.id).all()
    # Group by class: this teacher's Year 7s and Year 8s are separate groups.
    by_class = {}
    for a in assignments:
        sp = a.student.student_profile
        by_class.setdefault(sp.school_class if sp and sp.school_class else None, []).append(a)
    groups = sorted(by_class.items(), key=lambda kv: (kv[0] is None, kv[0].sort_order if kv[0] else 0,
                                                       kv[0].name if kv[0] else ""))
    return render_template("teachers/my_students.html", profile=profile, assignments=assignments,
                           groups=groups)


@teachers_bp.route("/teachers/me/sessions")
@login_required
def my_sessions():
    """The sessions this teacher hosts: timetabled lessons for their classes and
    sessions the admin set up for them. They open the room and admit the guests."""
    if not current_user.is_approved_teacher:
        abort(403)
    upcoming = Meeting.query.filter(
        Meeting.host_id == current_user.id, Meeting.status.in_(["scheduled", "live"])
    ).all()
    upcoming.sort(key=lambda m: m.scheduled_time or m.created_at)
    past = (
        Meeting.query.filter(Meeting.host_id == current_user.id, Meeting.status == "ended")
        .order_by(Meeting.ended_at.desc()).limit(15).all()
    )
    return render_template("teachers/my_sessions.html", upcoming=upcoming, past=past)
