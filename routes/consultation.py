"""How anyone other than the admin gets a consultation or a meeting.

There is no marketplace, no consultant directory and no way to start a
meeting yourself. A person *asks* the admin; the admin reviews the request and
either approves it (which creates the session, hosted by the admin, with the
requester invited) or declines it. See routes/admin.py for the decision side.
"""
from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user

from extensions import db
from models import MeetingRequest, REQUEST_KINDS

consult_bp = Blueprint("consult", __name__)

# Stops one account flooding the admin's queue.
MAX_OPEN_REQUESTS = 5


@consult_bp.route("/consult", methods=["GET", "POST"])
@login_required
def new():
    # The admin doesn't ask themselves — they schedule sessions directly.
    if current_user.is_admin:
        return redirect(url_for("admin.new_meeting"))

    valid_kinds = {k for k, _ in REQUEST_KINDS}

    if request.method == "POST":
        kind = request.form.get("kind", "")
        topic = request.form.get("topic", "").strip()
        details = request.form.get("details", "").strip()
        preferred_raw = request.form.get("preferred_time", "").strip()

        if kind not in valid_kinds:
            flash("Choose what kind of session you need.", "error")
            return render_template("consult/new.html", kinds=REQUEST_KINDS, form=request.form)
        if not topic:
            flash("Please tell the administrator what the session is about.", "error")
            return render_template("consult/new.html", kinds=REQUEST_KINDS, form=request.form)

        open_count = MeetingRequest.query.filter_by(requester_id=current_user.id, status="pending").count()
        if open_count >= MAX_OPEN_REQUESTS:
            flash("You already have several requests waiting. Please wait for the administrator to respond.", "error")
            return redirect(url_for("consult.mine"))

        preferred_time = None
        if preferred_raw:
            try:
                preferred_time = datetime.fromisoformat(preferred_raw)
            except ValueError:
                preferred_time = None

        db.session.add(MeetingRequest(
            requester_id=current_user.id,
            kind=kind,
            topic=topic[:200],
            details=details[:4000],
            preferred_time=preferred_time,
            status="pending",
        ))
        db.session.commit()
        flash("Request sent. Nothing is scheduled until the administrator approves it — you'll see the decision here.", "success")
        return redirect(url_for("consult.mine"))

    return render_template("consult/new.html", kinds=REQUEST_KINDS, form={})


@consult_bp.route("/consult/mine")
@login_required
def mine():
    requests_ = (
        MeetingRequest.query.filter_by(requester_id=current_user.id)
        .order_by(MeetingRequest.created_at.desc()).all()
    )
    return render_template("consult/mine.html", requests=requests_)


@consult_bp.route("/consult/<int:request_id>/cancel", methods=["POST"])
@login_required
def cancel(request_id):
    req = MeetingRequest.query.get_or_404(request_id)
    if req.requester_id != current_user.id:
        # Same answer as a missing request — don't confirm it exists.
        return redirect(url_for("consult.mine"))
    if req.status != "pending":
        flash("Only a request that is still waiting can be withdrawn. Contact the administrator to change an approved session.", "error")
        return redirect(url_for("consult.mine"))
    req.status = "cancelled"
    req.decided_at = datetime.utcnow()
    db.session.commit()
    flash("Request withdrawn.", "info")
    return redirect(url_for("consult.mine"))
