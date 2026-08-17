from datetime import datetime

from flask import (
    Blueprint, render_template, redirect, url_for, request, flash, abort, current_app,
    send_from_directory,
)
from flask_login import login_required, current_user

import storage
from extensions import db
from models import ConsultantProfile, VerificationDocument, User, Meeting, Appointment
from permissions import admin_required

admin_bp = Blueprint("admin", __name__)


# ---------------------------------------------------------------------------
# Consultant verification queue
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/consultants")
@login_required
@admin_required
def queue():
    pending = (
        ConsultantProfile.query.filter_by(status="pending")
        .order_by(ConsultantProfile.submitted_at.asc()).all()
    )
    reviewed = (
        ConsultantProfile.query.filter(ConsultantProfile.status.in_(["approved", "rejected"]))
        .order_by(ConsultantProfile.reviewed_at.desc()).limit(30).all()
    )
    return render_template("admin/queue.html", pending=pending, reviewed=reviewed)


@admin_bp.route("/admin/consultants/<int:profile_id>")
@login_required
@admin_required
def review(profile_id):
    profile = ConsultantProfile.query.get_or_404(profile_id)
    return render_template("admin/review.html", profile=profile)


@admin_bp.route("/admin/consultants/<int:profile_id>/approve", methods=["POST"])
@login_required
@admin_required
def approve(profile_id):
    profile = ConsultantProfile.query.get_or_404(profile_id)
    profile.status = "approved"
    profile.review_note = None
    profile.reviewed_at = datetime.utcnow()
    profile.reviewed_by_id = current_user.id
    db.session.commit()
    flash(f"{profile.user.name} is now a verified consultant.", "success")
    return redirect(url_for("admin.queue"))


@admin_bp.route("/admin/consultants/<int:profile_id>/reject", methods=["POST"])
@login_required
@admin_required
def reject(profile_id):
    profile = ConsultantProfile.query.get_or_404(profile_id)
    profile.status = "rejected"
    profile.review_note = request.form.get("review_note", "").strip()
    profile.reviewed_at = datetime.utcnow()
    profile.reviewed_by_id = current_user.id
    db.session.commit()
    flash(f"{profile.user.name}'s application was rejected.", "info")
    return redirect(url_for("admin.queue"))


@admin_bp.route("/admin/documents/<int:document_id>")
@login_required
def view_document(document_id):
    # Deliberately not @admin_required alone — the consultant who uploaded
    # a document also needs to be able to see their own file. Access is
    # otherwise fully locked down: nobody else, no public /static path.
    doc = VerificationDocument.query.get_or_404(document_id)
    profile = doc.profile
    if not (current_user.is_admin or profile.user_id == current_user.id):
        abort(403)

    bucket = current_app.config["VERIFICATION_BUCKET"]
    url = storage.signed_url(bucket, doc.stored_filename, expires_in=60)
    if url:
        return redirect(url)

    # Local-fallback mode (no Supabase configured) — serve directly.
    local_dir = current_app.config["VERIFICATION_DOCS_DIR"]
    return send_from_directory(local_dir, doc.stored_filename, as_attachment=False)


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
    meetings_hosted_count = Meeting.query.filter_by(host_id=user.id).count()
    appointments_as_client = Appointment.query.filter_by(client_id=user.id).count()
    appointments_as_consultant = Appointment.query.filter_by(consultant_id=user.id).count()
    return render_template(
        "admin/user_detail.html",
        user=user,
        meetings_hosted_count=meetings_hosted_count,
        appointments_as_client=appointments_as_client,
        appointments_as_consultant=appointments_as_consultant,
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


@admin_bp.route("/admin/users/<int:user_id>/toggle-premium", methods=["POST"])
@login_required
@admin_required
def toggle_premium(user_id):
    user = User.query.get_or_404(user_id)
    user.is_premium = not user.is_premium
    db.session.commit()
    flash(f"{user.name} is now {'premium' if user.is_premium else 'on the free tier'}.", "success")
    return redirect(url_for("admin.user_detail", user_id=user_id))
