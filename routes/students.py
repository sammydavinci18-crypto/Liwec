from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app
from flask_login import login_required, current_user

import storage
from extensions import db
from models import StudentProfile, StudentVerificationDocument, StudentTeacherAssignment, STUDENT_PROGRAMS

students_bp = Blueprint("students", __name__)

ALLOWED_DOC_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
MAX_DOCS_PER_APPLICATION = 5


def _allowed_doc(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_DOC_EXTENSIONS


@students_bp.route("/students/apply", methods=["GET", "POST"])
@login_required
def apply():
    existing = current_user.student_profile

    if request.method == "POST":
        dob_raw = request.form.get("date_of_birth", "").strip()
        desired_stage = request.form.get("desired_stage", "").strip()
        subjects = request.form.get("subjects_of_interest", "").strip()
        notes = request.form.get("notes", "").strip()

        valid_stage_keys = {k for k, _ in STUDENT_PROGRAMS}
        if desired_stage not in valid_stage_keys:
            desired_stage = None

        dob = None
        if dob_raw:
            try:
                dob = datetime.strptime(dob_raw, "%Y-%m-%d").date()
            except ValueError:
                dob = None

        files = [f for f in request.files.getlist("documents") if f and f.filename]
        if not existing and not files:
            flash("Please attach at least one document (ID, birth certificate, or previous school record) so an admin can review the application.", "error")
            return render_template("students/apply.html", existing=existing, stages=STUDENT_PROGRAMS)

        for f in files:
            if not _allowed_doc(f.filename):
                flash(f"'{f.filename}' isn't a supported file type (PDF, PNG, or JPG only).", "error")
                return render_template("students/apply.html", existing=existing, stages=STUDENT_PROGRAMS)

        if existing:
            profile = existing
            profile.date_of_birth = dob
            profile.desired_stage = desired_stage
            profile.subjects_of_interest = subjects
            profile.notes = notes
            if profile.status == "rejected":
                profile.status = "pending"
                profile.review_note = None
                profile.submitted_at = datetime.utcnow()
        else:
            profile = StudentProfile(
                user_id=current_user.id,
                date_of_birth=dob,
                desired_stage=desired_stage,
                subjects_of_interest=subjects,
                notes=notes,
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
                current_app.logger.error(f"Student document upload failed: {exc}")
                flash("Couldn't upload that document — please try again.", "error")
                return render_template("students/apply.html", existing=existing, stages=STUDENT_PROGRAMS)
            db.session.add(StudentVerificationDocument(
                profile_id=profile.id,
                stored_filename=stored_name,
                original_filename=f.filename,
            ))

        db.session.commit()
        flash("Application submitted. An admin will review it before you're enrolled.", "success")
        return redirect(url_for("students.my_profile"))

    return render_template("students/apply.html", existing=existing, stages=STUDENT_PROGRAMS)


@students_bp.route("/students/me")
@login_required
def my_profile():
    profile = current_user.student_profile
    if not profile:
        return redirect(url_for("students.apply"))

    assignments = StudentTeacherAssignment.query.filter_by(student_id=current_user.id).all()
    return render_template("students/my_profile.html", profile=profile, assignments=assignments)


@students_bp.route("/students/me/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    profile = current_user.student_profile
    if not profile:
        return redirect(url_for("students.apply"))

    if request.method == "POST":
        dob_raw = request.form.get("date_of_birth", "").strip()
        if dob_raw:
            try:
                profile.date_of_birth = datetime.strptime(dob_raw, "%Y-%m-%d").date()
            except ValueError:
                pass
        desired_stage = request.form.get("desired_stage", "").strip()
        valid_stage_keys = {k for k, _ in STUDENT_PROGRAMS}
        if desired_stage in valid_stage_keys:
            profile.desired_stage = desired_stage
        profile.subjects_of_interest = request.form.get("subjects_of_interest", "").strip()
        profile.notes = request.form.get("notes", "").strip()
        db.session.commit()
        flash("Application details updated.", "success")
        return redirect(url_for("students.my_profile"))

    return render_template("students/edit_profile.html", profile=profile, stages=STUDENT_PROGRAMS)
