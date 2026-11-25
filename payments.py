"""Pay-per-lesson: Pesapal (API 3.0) checkout, confirmation, lesson creation.

Flow
  1. start_checkout()   -> LessonPayment(pending) + a Pesapal hosted checkout URL
  2. the teacher pays on Pesapal's page (MTN / Airtel mobile money or card)
  3. Pesapal calls our IPN URL and sends the teacher back to our callback URL
  4. refresh_payment()  -> asks Pesapal for the real status (never trusts the
                           browser), and on COMPLETED creates the lesson, the
                           activation code and the student link, and emails them.

Secrets come from environment variables only (see config.py).
"""
import secrets
import smtplib
import threading
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from math import ceil

import requests
from flask import current_app, request, url_for

from extensions import db
from models import LessonPayment, Meeting, SiteSetting

LIVE_BASE = "https://pay.pesapal.com/v3"
SANDBOX_BASE = "https://cybqa.pesapal.com/pesapalv3"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I confusion

_token = {"value": None, "expires": 0.0}
_token_lock = threading.Lock()


class PaymentError(Exception):
    """Something the teacher can be told in plain words."""


# --------------------------------------------------------------------------- config / price

def configured():
    cfg = current_app.config
    return bool(cfg["PESAPAL_CONSUMER_KEY"] and cfg["PESAPAL_CONSUMER_SECRET"])


def _base():
    return SANDBOX_BASE if current_app.config["PESAPAL_ENV"] == "sandbox" else LIVE_BASE


def get_setting(key, default=None):
    row = db.session.get(SiteSetting, key)
    return row.value if row else default


def set_setting(key, value):
    row = db.session.get(SiteSetting, key)
    if row is None:
        row = SiteSetting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    db.session.commit()


def usd_ugx_rate():
    try:
        rate = float(get_setting("usd_ugx_rate", ""))
        if rate > 0:
            return rate
    except (TypeError, ValueError):
        pass
    return current_app.config["DEFAULT_USD_UGX_RATE"]


def lesson_price_ugx():
    """The lesson price in whole shillings, rounded UP to the next 100."""
    usd = current_app.config["LESSON_PRICE_USD"]
    return int(ceil(usd * usd_ugx_rate() / 100.0) * 100)


def _public_url(endpoint, **values):
    """Absolute URL Pesapal can reach (PUBLIC_BASE_URL wins, else this request's host)."""
    path = url_for(endpoint, **values)
    base = current_app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")
    return base + path


# --------------------------------------------------------------------------- Pesapal HTTP

def _auth_token():
    with _token_lock:
        if _token["value"] and time.time() < _token["expires"]:
            return _token["value"]
        cfg = current_app.config
        try:
            r = requests.post(
                _base() + "/api/Auth/RequestToken",
                json={"consumer_key": cfg["PESAPAL_CONSUMER_KEY"],
                      "consumer_secret": cfg["PESAPAL_CONSUMER_SECRET"]},
                headers={"Accept": "application/json"}, timeout=25,
            )
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            current_app.logger.error("Pesapal token request failed: %s", exc)
            raise PaymentError("Could not reach the payment service. Please try again in a minute.")
        token = data.get("token")
        if not token:
            current_app.logger.error("Pesapal refused the keys: %s", data.get("error") or data)
            raise PaymentError("The payment service rejected our login. The school administrator has been "
                               "notified; please try again later.")
        _token["value"] = token
        _token["expires"] = time.time() + 240    # Pesapal tokens last about 5 minutes
        return token


def _call(method, path, **kw):
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "Authorization": "Bearer " + _auth_token()}
    try:
        r = requests.request(method, _base() + path, headers=headers, timeout=25, **kw)
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        current_app.logger.error("Pesapal call %s failed: %s", path, exc)
        raise PaymentError("Could not reach the payment service. Please try again in a minute.")


def _ipn_id(ipn_url):
    """Registers our notification URL with Pesapal once and remembers its id."""
    key = "pesapal_ipn:" + ipn_url
    known = get_setting(key)
    if known:
        return known
    data = _call("POST", "/api/URLSetup/RegisterIPN", json={"url": ipn_url, "ipn_notification_type": "GET"})
    ipn_id = data.get("ipn_id")
    if not ipn_id:
        current_app.logger.error("Pesapal IPN registration failed: %s", data)
        raise PaymentError("Payment setup is incomplete. Please try again later.")
    set_setting(key, ipn_id)
    return ipn_id


# --------------------------------------------------------------------------- checkout

def start_checkout(user, title):
    """Creates the pending payment and returns Pesapal's checkout URL."""
    if not configured():
        raise PaymentError("Payments are not switched on yet.")
    title = (title or "").strip()[:200] or "Live lesson"
    amount = lesson_price_ugx()

    pay = LessonPayment(
        user_id=user.id, title=title, amount_ugx=amount, rate_used=usd_ugx_rate(),
        merchant_reference="LW" + secrets.token_hex(10).upper(),
    )
    db.session.add(pay)
    db.session.commit()

    name_parts = (user.name or "Teacher").split(None, 1)
    body = {
        "id": pay.merchant_reference,
        "currency": "UGX",
        "amount": amount,
        "description": "Liwec live lesson (recording included)"[:100],
        "callback_url": _public_url("lessons.pay_return"),
        "notification_id": _ipn_id(_public_url("lessons.pay_ipn")),
        "billing_address": {
            "email_address": user.email,
            "country_code": "UG",
            "first_name": name_parts[0][:50],
            "last_name": (name_parts[1] if len(name_parts) > 1 else "-")[:50],
        },
    }
    try:
        data = _call("POST", "/api/Transactions/SubmitOrderRequest", json=body)
    except PaymentError:
        pay.status = "failed"
        db.session.commit()
        raise
    url = data.get("redirect_url")
    if not url:
        current_app.logger.error("Pesapal order refused: %s", data)
        pay.status = "failed"
        db.session.commit()
        raise PaymentError("The payment service could not start this payment. Please try again.")
    pay.order_tracking_id = data.get("order_tracking_id")
    pay.checkout_url = url
    db.session.commit()
    return pay


# --------------------------------------------------------------------------- confirmation

def refresh_payment(pay):
    """Asks Pesapal what really happened and updates the payment. Safe to call
    repeatedly (the return page and the IPN both do)."""
    if pay.status != "pending" or not pay.order_tracking_id:
        return pay.status
    data = _call("GET", "/api/Transactions/GetTransactionStatus",
                 params={"orderTrackingId": pay.order_tracking_id})
    code = data.get("status_code")
    if code == 1:
        same_ref = data.get("merchant_reference") in (None, pay.merchant_reference)
        same_cur = str(data.get("currency") or "UGX").upper() == "UGX"
        try:
            enough = float(data.get("amount") or 0) + 0.5 >= pay.amount_ugx
        except (TypeError, ValueError):
            enough = False
        if same_ref and same_cur and enough:
            _mark_paid(pay.id, data)
        else:
            current_app.logger.error("Pesapal payment %s did not match the order: %s", pay.id, data)
    elif code in (2, 3):
        pay.status = "failed"
        db.session.commit()
    return db.session.get(LessonPayment, pay.id).status


def _new_code():
    while True:
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        code = "LW-%s-%s" % (raw[:4], raw[4:])
        if not LessonPayment.query.filter_by(activation_code=code).first():
            return code


def _mark_paid(pay_id, data):
    # Row lock: the IPN and the return page can arrive together; only one may
    # create the lesson. (SQLite ignores the lock but runs a single writer.)
    pay = db.session.query(LessonPayment).filter_by(id=pay_id).with_for_update().first()
    if pay is None or pay.status == "paid":
        db.session.rollback()
        return
    now = datetime.utcnow()
    meeting = Meeting(
        title=pay.title, host_id=pay.user_id, scheduled_time=now, status="scheduled",
        meeting_type="tutorial", is_independent=True,
    )
    db.session.add(meeting)
    db.session.flush()
    pay.meeting_id = meeting.id
    pay.status = "paid"
    pay.paid_at = now
    pay.payment_method = (data.get("payment_method") or "")[:60] or None
    pay.confirmation_code = (data.get("confirmation_code") or "")[:80] or None
    pay.activation_code = _new_code()
    pay.code_expires_at = now + timedelta(hours=current_app.config["ACTIVATION_CODE_HOURS"])
    db.session.commit()

    send_activation_email(pay)


# --------------------------------------------------------------------------- email

def student_link(meeting):
    return _public_url("guest.join", token=meeting.guest_token)


def send_activation_email(pay):
    cfg = current_app.config
    if not (cfg["SMTP_HOST"] and cfg["MAIL_FROM"]):
        return
    link = student_link(pay.meeting)
    lessons_url = _public_url("lessons.index")
    to = pay.user.email
    name = pay.user.name
    body = (
        "Hello %s,\n\nYour payment for \"%s\" was received.\n\n"
        "Activation code: %s\n"
        "Student link (send this to your students): %s\n\n"
        "Start the lesson from your lessons page: %s\n"
        "The code works until %s UTC. The lesson is recorded automatically, and the recording is kept for %d days.\n\n"
        "Liwec"
    ) % (name, pay.title, pay.activation_code, link, lessons_url,
         pay.code_expires_at.strftime("%d %b %Y %H:%M"), cfg["LESSON_RETENTION_DAYS"])
    app = current_app._get_current_object()

    def _send():
        try:
            msg = EmailMessage()
            msg["Subject"] = "Your Liwec lesson is ready"
            msg["From"] = app.config["MAIL_FROM"]
            msg["To"] = to
            msg.set_content(body)
            with smtplib.SMTP(app.config["SMTP_HOST"], app.config["SMTP_PORT"], timeout=20) as s:
                s.starttls()
                if app.config["SMTP_USER"]:
                    s.login(app.config["SMTP_USER"], app.config["SMTP_PASSWORD"])
                s.send_message(msg)
        except Exception as exc:  # never let email break a payment
            app.logger.error("Activation email failed: %s", exc)

    threading.Thread(target=_send, daemon=True).start()
