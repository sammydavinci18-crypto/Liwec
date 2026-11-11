"""The school portal: one place for students and parents to see the school
record (application status, teachers, classes, reports), plus the timetable &
calendar, announcements, and teachers' progress reports."""
from collections import OrderedDict

from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user

from extensions import db
from models import ProgressReport, StudentTeacherAssignment, User
from school import (
    approved_children, can_view_student, reports_for, student_summary,
    teacher_has_student, upcoming_events, upcoming_sessions_for, visible_announcements,
)

portal_bp = Blueprint("portal", __name__)


@portal_bp.route("/school")
@login_required
def index():
    """My school: the student's own record and/or each linked child's."""
    summaries = []
    if current_user.student_profile:
        summaries.append(dict(student_summary(current_user), is_self=True))
    for child in approved_children(current_user):
        summaries.append(dict(student_summary(child), is_self=False))
    return render_template(
        "portal/index.html",
        summaries=summaries,
        announcements=visible_announcements(current_user, limit=5),
    )


@portal_bp.route("/timetable")
@login_required
def timetable():
    """Upcoming sessions grouped by day (mine, and my children's), alongside
    the school calendar."""
    entries = [(m, None) for m in upcoming_sessions_for(current_user.id, include_hosted=current_user.is_admin)]
    for child in approved_children(current_user):
        entries += [(m, child) for m in upcoming_sessions_for(child.id)]
    entries.sort(key=lambda e: e[0].scheduled_time or e[0].created_at)

    days = OrderedDict()
    for meeting, child in entries:
        when = meeting.scheduled_time or meeting.created_at
        days.setdefault(when.date(), []).append((meeting, child))

    return render_template("portal/timetable.html", days=days, events=upcoming_events(current_user))


@portal_bp.route("/announcements")
@login_required
def announcements():
    return render_template("portal/announcements.html", items=visible_announcements(current_user))


# ---------------------------------------------------------------------------
# Progress reports
# ---------------------------------------------------------------------------

@portal_bp.route("/reports/new", methods=["GET", "POST"])
@login_required
def new_report():
    """A teacher writes a report on a student the admin has placed with them."""
    placements = StudentTeacherAssignment.query.filter_by(teacher_id=current_user.id).all()
    if not placements:
        flash("You can write reports once students have been placed with you.", "info")
        return redirect(url_for("teachers.my_students"))

    if request.method == "POST":
        student_id = request.form.get("student_id", type=int)
        term = request.form.get("term", "").strip()
        subject = request.form.get("subject", "").strip()
        summary = request.form.get("summary", "").strip()
        if not student_id or not teacher_has_student(current_user.id, student_id):
            abort(403)
        if not term or not subject or not summary:
            flash("Term, subject and a summary are required.", "error")
            return render_template("reports/new.html", placements=placements, form=request.form)
        report = ProgressReport(
            student_id=student_id, teacher_id=current_user.id,
            term=term[:60], subject=subject[:120],
            grade=request.form.get("grade", "").strip()[:30] or None,
            summary=summary,
            strengths=request.form.get("strengths", "").strip() or None,
            next_steps=request.form.get("next_steps", "").strip() or None,
        )
        db.session.add(report)
        db.session.commit()
        flash("Report saved. The student and their linked parents can now read it.", "success")
        return redirect(url_for("portal.view_report", report_id=report.id))

    preselect = request.args.get("student_id", type=int)
    return render_template("reports/new.html", placements=placements,
                           form={"student_id": str(preselect) if preselect else ""})


@portal_bp.route("/reports/<int:report_id>")
@login_required
def view_report(report_id):
    report = ProgressReport.query.get_or_404(report_id)
    if not can_view_student(current_user, report.student_id):
        abort(404)
    return render_template("reports/view.html", report=report)


@portal_bp.route("/reports/student/<int:student_id>")
@login_required
def student_reports(student_id):
    if not can_view_student(current_user, student_id):
        abort(404)
    student = User.query.get_or_404(student_id)
    return render_template("reports/list.html", student=student, reports=reports_for(student_id))
