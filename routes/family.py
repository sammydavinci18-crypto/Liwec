from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user
from sqlalchemy import or_

from extensions import db
from models import User, ParentStudentLink

family_bp = Blueprint("family", __name__)


@family_bp.route("/family")
@login_required
def dashboard():
    as_parent = (
        ParentStudentLink.query.filter_by(parent_id=current_user.id)
        .order_by(ParentStudentLink.created_at.desc()).all()
    )
    incoming_requests = (
        ParentStudentLink.query.filter_by(student_id=current_user.id, status="pending")
        .order_by(ParentStudentLink.created_at.desc()).all()
    )
    approved_children = [link for link in as_parent if link.status == "approved"]
    return render_template(
        "family/dashboard.html",
        as_parent=as_parent,
        incoming_requests=incoming_requests,
        approved_children=approved_children,
    )


@family_bp.route("/family/request", methods=["POST"])
@login_required
def request_link():
    student_email = request.form.get("student_email", "").strip().lower()
    student = User.query.filter_by(email=student_email).first()

    if not student:
        flash("No account found with that email.", "error")
        return redirect(url_for("family.dashboard"))
    if student.id == current_user.id:
        flash("You can't link yourself as your own child.", "error")
        return redirect(url_for("family.dashboard"))

    existing = ParentStudentLink.query.filter_by(parent_id=current_user.id, student_id=student.id).first()
    if existing:
        flash(f"You already have a {existing.status} link with that account.", "info")
        return redirect(url_for("family.dashboard"))

    link = ParentStudentLink(
        parent_id=current_user.id, student_id=student.id, status="pending", requested_by="parent"
    )
    db.session.add(link)
    db.session.commit()
    flash(f"Request sent to {student.name}. They'll need to approve it before you can see anything.", "success")
    return redirect(url_for("family.dashboard"))


@family_bp.route("/family/<int:link_id>/approve", methods=["POST"])
@login_required
def approve_link(link_id):
    link = ParentStudentLink.query.get_or_404(link_id)
    if link.student_id != current_user.id:
        abort(403)
    link.status = "approved"
    link.approved_at = datetime.utcnow()
    db.session.commit()
    flash(f"{link.parent.name} is now linked as your parent/guardian.", "success")
    return redirect(url_for("family.dashboard"))


@family_bp.route("/family/<int:link_id>/decline", methods=["POST"])
@login_required
def decline_link(link_id):
    link = ParentStudentLink.query.get_or_404(link_id)
    if link.student_id != current_user.id:
        abort(403)
    link.status = "declined"
    db.session.commit()
    flash("Request declined.", "info")
    return redirect(url_for("family.dashboard"))


@family_bp.route("/family/<int:link_id>/unlink", methods=["POST"])
@login_required
def unlink(link_id):
    link = ParentStudentLink.query.get_or_404(link_id)
    if current_user.id not in (link.parent_id, link.student_id) and not (current_user.is_admin or current_user.is_registrar):
        abort(403)
    db.session.delete(link)
    db.session.commit()
    flash("Link removed.", "info")
    return redirect(url_for("family.dashboard"))


# ---------------------------------------------------------------------------
# Registrar: override linking (e.g. for younger children who can't self-approve)
# ---------------------------------------------------------------------------

@family_bp.route("/registrar/link", methods=["POST"])
@login_required
def registrar_create_link():
    if not (current_user.is_registrar or current_user.is_admin):
        abort(403)

    parent_email = request.form.get("parent_email", "").strip().lower()
    student_email = request.form.get("student_email", "").strip().lower()
    parent = User.query.filter_by(email=parent_email).first()
    student = User.query.filter_by(email=student_email).first()

    if not parent or not student:
        flash("Couldn't find one or both of those accounts.", "error")
        return redirect(url_for("family.registrar_dashboard"))
    if parent.id == student.id:
        flash("Parent and student can't be the same account.", "error")
        return redirect(url_for("family.registrar_dashboard"))

    existing = ParentStudentLink.query.filter_by(parent_id=parent.id, student_id=student.id).first()
    if existing:
        existing.status = "approved"
        existing.requested_by = "registrar"
        existing.approved_at = datetime.utcnow()
    else:
        db.session.add(ParentStudentLink(
            parent_id=parent.id, student_id=student.id, status="approved",
            requested_by="registrar", approved_at=datetime.utcnow(),
        ))
    db.session.commit()
    flash(f"Linked {parent.name} as parent/guardian of {student.name}.", "success")
    return redirect(url_for("family.registrar_dashboard"))


@family_bp.route("/registrar")
@login_required
def registrar_dashboard():
    if not (current_user.is_registrar or current_user.is_admin):
        abort(403)

    q = request.args.get("q", "").strip()
    query = User.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(User.name.ilike(like), User.email.ilike(like)))
    users = query.order_by(User.created_at.desc()).limit(100).all()

    pending_links = (
        ParentStudentLink.query.filter_by(status="pending")
        .order_by(ParentStudentLink.created_at.desc()).all()
    )
    return render_template("family/registrar_dashboard.html", users=users, q=q, pending_links=pending_links)


@family_bp.route("/registrar/links/<int:link_id>/approve", methods=["POST"])
@login_required
def registrar_approve_link(link_id):
    if not (current_user.is_registrar or current_user.is_admin):
        abort(403)
    link = ParentStudentLink.query.get_or_404(link_id)
    link.status = "approved"
    link.approved_at = datetime.utcnow()
    db.session.commit()
    flash("Link approved.", "success")
    return redirect(url_for("family.registrar_dashboard"))
