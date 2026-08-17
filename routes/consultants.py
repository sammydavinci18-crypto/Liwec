from datetime import datetime

from flask import (
    Blueprint, render_template, redirect, url_for, request, flash, abort, current_app,
)
from flask_login import login_required, current_user

import storage
from extensions import db
from models import (
    User, ConsultantProfile, VerificationDocument, AvailabilitySlot, Appointment, Conversation,
)

consultants_bp = Blueprint("consultants", __name__)

ALLOWED_DOC_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
MAX_DOCS_PER_APPLICATION = 5


def _allowed_doc(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_DOC_EXTENSIONS


# ---------------------------------------------------------------------------
# Directory — browse approved consultants
# ---------------------------------------------------------------------------

@consultants_bp.route("/consultants")
@login_required
def directory():
    vertical = request.args.get("vertical", "all")
    query = ConsultantProfile.query.filter_by(status="approved")
    if vertical in ("education", "business"):
        query = query.filter(ConsultantProfile.vertical.in_([vertical, "both"]))
    profiles = query.order_by(ConsultantProfile.submitted_at.desc()).all()
    return render_template("consultants/directory.html", profiles=profiles, vertical=vertical)


@consultants_bp.route("/consultants/<int:user_id>")
@login_required
def profile(user_id):
    profile = ConsultantProfile.query.filter_by(user_id=user_id).first_or_404()
    if profile.status != "approved" and profile.user_id != current_user.id and not current_user.is_admin:
        abort(404)

    open_slots = [
        s for s in profile.availability_slots
        if not s.is_booked and s.start_time > datetime.utcnow()
    ]
    return render_template("consultants/profile.html", profile=profile, open_slots=open_slots)


# ---------------------------------------------------------------------------
# Becoming a consultant — application with verification documents
# ---------------------------------------------------------------------------

@consultants_bp.route("/consultants/apply", methods=["GET", "POST"])
@login_required
def apply():
    existing = current_user.consultant_profile

    if request.method == "POST":
        headline = request.form.get("headline", "").strip()
        bio = request.form.get("bio", "").strip()
        vertical = request.form.get("vertical", "education")
        if vertical not in ("education", "business", "both"):
            vertical = "education"
        price_raw = request.form.get("price_amount", "").strip()
        price_unit = request.form.get("price_unit", "session")
        if price_unit not in ("session", "hour"):
            price_unit = "session"

        try:
            price_amount = float(price_raw) if price_raw else None
        except ValueError:
            price_amount = None

        files = [f for f in request.files.getlist("documents") if f and f.filename]
        if not existing and not files:
            flash("Please attach at least one verification document (ID or certificate).", "error")
            return render_template("consultants/apply.html", existing=existing)

        for f in files:
            if not _allowed_doc(f.filename):
                flash(f"'{f.filename}' isn't a supported file type (PDF, PNG, or JPG only).", "error")
                return render_template("consultants/apply.html", existing=existing)

        if existing:
            profile = existing
            profile.headline = headline
            profile.bio = bio
            profile.vertical = vertical
            profile.price_amount = price_amount
            profile.price_unit = price_unit
            # Re-applying after a rejection puts them back in the review queue.
            if profile.status == "rejected":
                profile.status = "pending"
                profile.review_note = None
                profile.submitted_at = datetime.utcnow()
        else:
            profile = ConsultantProfile(
                user_id=current_user.id,
                headline=headline,
                bio=bio,
                vertical=vertical,
                price_amount=price_amount,
                price_unit=price_unit,
                status="pending",
            )
            db.session.add(profile)
            db.session.flush()  # get profile.id for the documents below

        for f in files[:MAX_DOCS_PER_APPLICATION]:
            stored_name = storage.new_object_key(f.filename)
            try:
                storage.upload_bytes(
                    current_app.config["VERIFICATION_BUCKET"], stored_name,
                    f.read(), content_type=f.mimetype or "application/octet-stream",
                )
            except storage.StorageError as exc:
                current_app.logger.error(f"Verification document upload failed: {exc}")
                flash("Couldn't upload that document — please try again.", "error")
                return render_template("consultants/apply.html", existing=existing)
            db.session.add(VerificationDocument(
                profile_id=profile.id,
                stored_filename=stored_name,
                original_filename=f.filename,
            ))

        if current_user.role == "client":
            current_user.role = "consultant"

        db.session.commit()
        flash("Application submitted. We'll review your documents and let you know.", "success")
        return redirect(url_for("consultants.my_profile"))

    return render_template("consultants/apply.html", existing=existing)


@consultants_bp.route("/consultants/me")
@login_required
def my_profile():
    profile = current_user.consultant_profile
    if not profile:
        return redirect(url_for("consultants.apply"))
    return render_template("consultants/my_profile.html", profile=profile)


@consultants_bp.route("/consultants/me/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    profile = current_user.consultant_profile
    if not profile:
        return redirect(url_for("consultants.apply"))

    if request.method == "POST":
        profile.headline = request.form.get("headline", "").strip()
        profile.bio = request.form.get("bio", "").strip()
        vertical = request.form.get("vertical", profile.vertical)
        if vertical in ("education", "business", "both"):
            profile.vertical = vertical
        price_raw = request.form.get("price_amount", "").strip()
        try:
            profile.price_amount = float(price_raw) if price_raw else None
        except ValueError:
            pass
        price_unit = request.form.get("price_unit", profile.price_unit)
        if price_unit in ("session", "hour"):
            profile.price_unit = price_unit

        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("consultants.my_profile"))

    return render_template("consultants/edit_profile.html", profile=profile)


# ---------------------------------------------------------------------------
# Availability (approved consultants only)
# ---------------------------------------------------------------------------

@consultants_bp.route("/consultants/me/availability", methods=["GET", "POST"])
@login_required
def availability():
    profile = current_user.consultant_profile
    if not profile or profile.status != "approved":
        abort(403)

    if request.method == "POST":
        start_raw = request.form.get("start_time", "").strip()
        end_raw = request.form.get("end_time", "").strip()
        try:
            start_time = datetime.fromisoformat(start_raw)
            end_time = datetime.fromisoformat(end_raw)
        except ValueError:
            flash("Enter a valid start and end time.", "error")
            return redirect(url_for("consultants.availability"))

        if end_time <= start_time:
            flash("The end time has to be after the start time.", "error")
        elif start_time < datetime.utcnow():
            flash("Pick a time in the future.", "error")
        else:
            db.session.add(AvailabilitySlot(profile_id=profile.id, start_time=start_time, end_time=end_time))
            db.session.commit()
            flash("Slot added.", "success")
        return redirect(url_for("consultants.availability"))

    upcoming = [s for s in profile.availability_slots if s.start_time > datetime.utcnow()]
    return render_template("consultants/availability.html", profile=profile, slots=upcoming)


@consultants_bp.route("/consultants/me/availability/<int:slot_id>/delete", methods=["POST"])
@login_required
def delete_availability(slot_id):
    profile = current_user.consultant_profile
    slot = AvailabilitySlot.query.get_or_404(slot_id)
    if not profile or slot.profile_id != profile.id:
        abort(403)
    if slot.is_booked:
        flash("Can't remove a slot that's already booked.", "error")
    else:
        db.session.delete(slot)
        db.session.commit()
        flash("Slot removed.", "success")
    return redirect(url_for("consultants.availability"))


# ---------------------------------------------------------------------------
# Booking a consultation
# ---------------------------------------------------------------------------

@consultants_bp.route("/consultants/<int:user_id>/book", methods=["POST"])
@login_required
def book(user_id):
    profile = ConsultantProfile.query.filter_by(user_id=user_id, status="approved").first_or_404()
    if profile.user_id == current_user.id:
        flash("You can't book a consultation with yourself.", "error")
        return redirect(url_for("consultants.profile", user_id=user_id))

    slot_id = request.form.get("slot_id", "").strip()
    custom_time_raw = request.form.get("requested_time", "").strip()
    note = request.form.get("note", "").strip()

    slot = None
    requested_time = None

    if slot_id:
        slot = AvailabilitySlot.query.filter_by(id=slot_id, profile_id=profile.id, is_booked=False).first()
        if not slot:
            flash("That slot is no longer available — pick another.", "error")
            return redirect(url_for("consultants.profile", user_id=user_id))
        requested_time = slot.start_time
    elif custom_time_raw:
        try:
            requested_time = datetime.fromisoformat(custom_time_raw)
        except ValueError:
            flash("Enter a valid date and time.", "error")
            return redirect(url_for("consultants.profile", user_id=user_id))
        if requested_time < datetime.utcnow():
            flash("Pick a time in the future.", "error")
            return redirect(url_for("consultants.profile", user_id=user_id))
    else:
        flash("Pick an open slot or propose a date and time.", "error")
        return redirect(url_for("consultants.profile", user_id=user_id))

    appointment = Appointment(
        consultant_id=profile.user_id,
        client_id=current_user.id,
        slot_id=slot.id if slot else None,
        requested_time=requested_time,
        price_amount=profile.price_amount,
        price_currency=profile.price_currency,
        client_note=note,
        status="pending",
    )
    if slot:
        slot.is_booked = True

    db.session.add(appointment)
    db.session.commit()
    flash("Request sent — you'll be notified once the consultant responds.", "success")
    return redirect(url_for("appointments.my_appointments"))


# ---------------------------------------------------------------------------
# "Message first" path — start (or resume) a conversation from a profile
# ---------------------------------------------------------------------------

@consultants_bp.route("/consultants/<int:user_id>/message", methods=["POST"])
@login_required
def start_conversation(user_id):
    profile = ConsultantProfile.query.filter_by(user_id=user_id, status="approved").first_or_404()
    if profile.user_id == current_user.id:
        abort(400)

    conversation = Conversation.query.filter_by(
        consultant_id=profile.user_id, client_id=current_user.id
    ).first()
    if not conversation:
        conversation = Conversation(consultant_id=profile.user_id, client_id=current_user.id)
        db.session.add(conversation)
        db.session.commit()

    return redirect(url_for("messages.thread", conversation_id=conversation.id))
