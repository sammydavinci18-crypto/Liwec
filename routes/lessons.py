"""Pay-per-lesson: any signed-in user can buy a $1 lesson and teach it, with no
admin approval. Payment goes through Pesapal (see payments.py)."""
from datetime import datetime

from flask import (
    Blueprint, render_template, redirect, url_for, request, flash, jsonify, current_app,
)
from flask_login import login_required, current_user

import payments
import recording_service
from extensions import db
from models import LessonPayment, Meeting
from permissions import admin_required

lessons_bp = Blueprint("lessons", __name__)


def _refresh_quietly(pay):
    try:
        payments.refresh_payment(pay)
    except payments.PaymentError:
        db.session.rollback()


@lessons_bp.route("/lessons")
@login_required
def index():
    mine = (LessonPayment.query.filter_by(user_id=current_user.id)
            .order_by(LessonPayment.created_at.desc()).limit(40).all())
    # A teacher who just paid may arrive before Pesapal's notification: ask directly.
    for pay in [p for p in mine if p.status == "pending" and p.order_tracking_id][:5]:
        _refresh_quietly(pay)

    ready, waiting = [], []
    for pay in mine:
        if pay.status == "paid" and pay.meeting and pay.meeting.status in ("scheduled", "live"):
            ready.append(pay)
        elif pay.status == "pending" and pay.checkout_url:
            waiting.append(pay)

    ended = recording_service.prepare_recordings_page(
        current_app._get_current_object(),
        lambda: Meeting.query.filter(Meeting.host_id == current_user.id, Meeting.is_independent.is_(True),
                                     Meeting.status == "ended")
        .order_by(Meeting.ended_at.desc(), Meeting.id.desc()).limit(50),
    )
    return render_template(
        "lessons/index.html", ready=ready, waiting=waiting, ended=ended,
        state=recording_service.safe_recording_state, price_ugx=payments.lesson_price_ugx(),
        now=datetime.utcnow(), student_link=payments.student_link,
    )


@lessons_bp.route("/lessons/new", methods=["GET", "POST"])
@login_required
def new():
    price = payments.lesson_price_ugx()
    if request.method == "POST":
        try:
            pay = payments.start_checkout(current_user, request.form.get("title"))
        except payments.PaymentError as exc:
            flash(str(exc), "error")
            return render_template("lessons/new.html", price_ugx=price, form=request.form,
                                   usd=current_app.config["LESSON_PRICE_USD"])
        return redirect(pay.checkout_url)
    return render_template("lessons/new.html", price_ugx=price, form={},
                           usd=current_app.config["LESSON_PRICE_USD"])


@lessons_bp.route("/lessons/pay/return")
def pay_return():
    """Where Pesapal sends the teacher after paying (or cancelling)."""
    ref = request.args.get("OrderMerchantReference", "")
    pay = LessonPayment.query.filter_by(merchant_reference=ref).first()
    if pay:
        _refresh_quietly(pay)
        if pay.status == "paid":
            flash("Payment received. Your activation code and student link are below.", "success")
        elif pay.status == "failed":
            flash("That payment did not go through. You have not been charged for a lesson.", "error")
        else:
            flash("We are still waiting for the payment to be confirmed. This page updates when you refresh it.", "info")
    return redirect(url_for("lessons.index"))


@lessons_bp.route("/lessons/pay/ipn", methods=["GET", "POST"])
def pay_ipn():
    """Pesapal's server-to-server notification. We ignore what it says and ask
    Pesapal for the real status, so a forged call can't unlock a lesson."""
    args = request.values
    tracking = args.get("OrderTrackingId", "")
    ref = args.get("OrderMerchantReference", "")
    pay = LessonPayment.query.filter_by(merchant_reference=ref).first()
    if pay and (not pay.order_tracking_id or pay.order_tracking_id == tracking):
        _refresh_quietly(pay)
    return jsonify(orderNotificationType=args.get("OrderNotificationType", "IPNCHANGE"),
                   orderTrackingId=tracking, orderMerchantReference=ref, status=200)


@lessons_bp.route("/lessons/activate", methods=["POST"])
@login_required
def activate():
    code = (request.form.get("code") or "").strip().upper().replace(" ", "")
    pay = LessonPayment.query.filter_by(activation_code=code).first() if code else None
    if not pay or pay.user_id != current_user.id or pay.status != "paid" or not pay.meeting:
        flash("That activation code was not found on your account.", "error")
        return redirect(url_for("lessons.index"))
    meeting = pay.meeting
    if meeting.status == "ended":
        flash("That lesson has already finished.", "error")
        return redirect(url_for("lessons.index"))
    if not meeting.is_hosted_by(current_user):
        flash("That activation code has expired. Please buy a new lesson.", "error")
        return redirect(url_for("lessons.index"))
    return redirect(url_for("meetings.room", room_code=meeting.room_code))


# ----------------------------------------------------------------------------- admin

@lessons_bp.route("/admin/payments", methods=["GET", "POST"])
@login_required
@admin_required
def admin_payments():
    if request.method == "POST":
        try:
            rate = float(request.form.get("rate", ""))
            if not 500 <= rate <= 20000:
                raise ValueError
        except ValueError:
            flash("Enter the shillings per US dollar as a number, for example 3700.", "error")
        else:
            payments.set_setting("usd_ugx_rate", rate)
            flash("Exchange rate saved. New lessons use it straight away.", "success")
        return redirect(url_for("lessons.admin_payments"))
    rows = LessonPayment.query.order_by(LessonPayment.created_at.desc()).limit(100).all()
    paid = [r for r in rows if r.status == "paid"]
    return render_template(
        "admin/payments.html", rows=rows, paid_count=len(paid),
        paid_total=sum(r.amount_ugx for r in paid), rate=payments.usd_ugx_rate(),
        price_ugx=payments.lesson_price_ugx(), configured=payments.configured(),
        env=current_app.config["PESAPAL_ENV"], ipn_url=payments._public_url("lessons.pay_ipn"),
    )
