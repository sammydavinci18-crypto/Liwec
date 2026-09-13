import secrets
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db


def generate_room_code():
    return secrets.token_urlsafe(6).replace("_", "").replace("-", "")[:8]


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    # "client" (default — regular user: books consultations, enrolls as a
    # student, can be linked as a parent), "registrar" (school-scoped staff:
    # manages enrollment/rosters, does NOT approve teacher applications —
    # that stays with admin), "admin" (reviews verification requests,
    # platform-wide). Being a consultant or teacher is NOT a role value —
    # it's determined by holding an approved ConsultantProfile/TeacherProfile,
    # same pattern as the existing consultant flow, so one person can hold
    # several of these at once (e.g. teacher AND parent).
    role = db.Column(db.String(20), default="client", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Moderation — a banned user is force-logged-out and can't log back in
    # (see load_user in app.py and the login check in routes/auth.py).
    is_banned = db.Column(db.Boolean, default=False, nullable=False)
    ban_reason = db.Column(db.Text)  # admin-only note, e.g. for legal/abuse records
    banned_at = db.Column(db.DateTime)

    # Premium — admin-granted (no payment processing), gates call length and
    # recording retention. See config.py for the actual limits.
    is_premium = db.Column(db.Boolean, default=False, nullable=False)

    meetings_hosted = db.relationship(
        "Meeting", backref="host", lazy=True, foreign_keys="Meeting.host_id"
    )
    consultant_profile = db.relationship(
        "ConsultantProfile", backref="user", uselist=False, cascade="all, delete-orphan",
        foreign_keys="ConsultantProfile.user_id",
    )
    teacher_profile = db.relationship(
        "TeacherProfile", backref="user", uselist=False, cascade="all, delete-orphan",
        foreign_keys="TeacherProfile.user_id",
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_registrar(self):
        return self.role == "registrar"


class Meeting(db.Model):
    __tablename__ = "meetings"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    room_code = db.Column(db.String(16), unique=True, nullable=False, default=generate_room_code, index=True)
    host_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    scheduled_time = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default="scheduled")  # scheduled, live, ended
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Set when the host actually joins (not when the row is created) — this
    # is what the server-side call-length limit counts from. See sockets.py.
    call_started_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)

    participants = db.relationship("MeetingParticipant", backref="meeting", lazy=True, cascade="all, delete-orphan")
    notes = db.relationship("ConsultationNote", backref="meeting", lazy=True, cascade="all, delete-orphan")


class MeetingParticipant(db.Model):
    __tablename__ = "meeting_participants"

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    left_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User")


class ConsultationNote(db.Model):
    __tablename__ = "consultation_notes"

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    author = db.relationship("User")


class Recording(db.Model):
    __tablename__ = "recordings"

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), unique=True, nullable=False)
    # Object key within the recordings storage bucket (Supabase Storage in
    # production; a local path under RECORDINGS_DIR in dev/fallback mode —
    # see storage.py). Not a filesystem path to rely on directly.
    filename = db.Column(db.String(255), nullable=False)
    finalized = db.Column(db.Boolean, default=False)  # True once the host has ended the meeting
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Retention — set once finalized, based on the host's plan at that time.
    expires_at = db.Column(db.DateTime, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True)  # set once the file's actually been removed

    meeting = db.relationship("Meeting", backref=db.backref("recording", uselist=False, cascade="all, delete-orphan"))

    @property
    def is_expired(self):
        return self.expires_at is not None and datetime.utcnow() >= self.expires_at


# =============================================================================
# Consultant marketplace: profiles, verification, availability, booking, chat
# =============================================================================

class ConsultantProfile(db.Model):
    __tablename__ = "consultant_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    headline = db.Column(db.String(160))
    bio = db.Column(db.Text)
    # "education" | "business" | "both" — matches the two consulting tracks
    # on the landing page, so the directory can filter the same way.
    vertical = db.Column(db.String(20), default="education", nullable=False)

    price_amount = db.Column(db.Numeric(10, 2))
    price_currency = db.Column(db.String(8), default="USD")
    price_unit = db.Column(db.String(20), default="session")  # "session" | "hour"

    # "pending" (awaiting admin review) | "approved" | "rejected"
    status = db.Column(db.String(20), default="pending", nullable=False)
    review_note = db.Column(db.Text)  # admin's note, shown to the consultant on rejection
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    documents = db.relationship(
        "VerificationDocument", backref="profile", lazy=True, cascade="all, delete-orphan"
    )
    availability_slots = db.relationship(
        "AvailabilitySlot", backref="profile", lazy=True, cascade="all, delete-orphan",
        order_by="AvailabilitySlot.start_time",
    )

    def price_display(self):
        if self.price_amount is None:
            return "Rate on request"
        unit = "hr" if self.price_unit == "hour" else "session"
        return f"{self.price_currency} {self.price_amount:.0f} / {unit}"


class VerificationDocument(db.Model):
    __tablename__ = "verification_documents"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("consultant_profiles.id"), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)  # random name on disk
    original_filename = db.Column(db.String(255), nullable=False)  # name to show the consultant/admin
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class AvailabilitySlot(db.Model):
    __tablename__ = "availability_slots"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("consultant_profiles.id"), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    is_booked = db.Column(db.Boolean, default=False, nullable=False)


class Appointment(db.Model):
    __tablename__ = "appointments"

    id = db.Column(db.Integer, primary_key=True)
    consultant_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    slot_id = db.Column(db.Integer, db.ForeignKey("availability_slots.id"), nullable=True)

    # Set when booked from an open slot; otherwise this is the client's
    # requested date/time for the consultant to confirm or renegotiate.
    requested_time = db.Column(db.DateTime, nullable=True)

    price_amount = db.Column(db.Numeric(10, 2))
    price_currency = db.Column(db.String(8), default="USD")
    client_note = db.Column(db.Text)

    # "pending" | "confirmed" | "declined" | "cancelled" | "completed"
    status = db.Column(db.String(20), default="pending", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    responded_at = db.Column(db.DateTime)

    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), nullable=True)

    consultant = db.relationship("User", foreign_keys=[consultant_id])
    client = db.relationship("User", foreign_keys=[client_id])
    slot = db.relationship("AvailabilitySlot")
    meeting = db.relationship("Meeting")

    def price_display(self):
        if self.price_amount is None:
            return "Not set yet"
        return f"{self.price_currency} {self.price_amount:.0f}"


class Conversation(db.Model):
    __tablename__ = "conversations"
    __table_args__ = (db.UniqueConstraint("consultant_id", "client_id", name="uq_conversation_pair"),)

    id = db.Column(db.Integer, primary_key=True)
    consultant_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    consultant = db.relationship("User", foreign_keys=[consultant_id])
    client = db.relationship("User", foreign_keys=[client_id])
    messages = db.relationship(
        "DirectMessage", backref="conversation", lazy=True, cascade="all, delete-orphan",
        order_by="DirectMessage.created_at",
    )

    def other_party(self, current_user_id):
        return self.client if current_user_id == self.consultant_id else self.consultant


class DirectMessage(db.Model):
    __tablename__ = "direct_messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    read_at = db.Column(db.DateTime)

    sender = db.relationship("User")


# =============================================================================
# Virtual school: teacher verification, and parent-student linking
# =============================================================================

CAMBRIDGE_STAGES = [
    ("primary", "Cambridge Primary"),
    ("lower_secondary", "Cambridge Lower Secondary"),
    ("igcse", "Cambridge IGCSE"),
    ("as_a_level", "Cambridge AS & A Level"),
]


class TeacherProfile(db.Model):
    __tablename__ = "teacher_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    headline = db.Column(db.String(160))
    bio = db.Column(db.Text)
    subjects = db.Column(db.String(255))  # comma-separated, e.g. "Mathematics, Physics"
    stages = db.Column(db.String(255))  # comma-separated stage keys, e.g. "igcse,as_a_level"

    # "independent" (default — runs their own classes on the public platform)
    # or "official" (a Light-Way-employed teacher). Only an admin can set
    # this to "official" — see routes/admin.py. Independent teachers go
    # through the exact same document-verification queue as official ones;
    # this flag is about employment status, not trust level.
    teacher_type = db.Column(db.String(20), default="independent", nullable=False)

    # "pending" | "approved" | "rejected" — same review flow as consultants.
    status = db.Column(db.String(20), default="pending", nullable=False)
    review_note = db.Column(db.Text)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    documents = db.relationship(
        "TeacherVerificationDocument", backref="profile", lazy=True, cascade="all, delete-orphan"
    )

    def subject_list(self):
        return [s.strip() for s in (self.subjects or "").split(",") if s.strip()]

    def stage_labels(self):
        keys = [s.strip() for s in (self.stages or "").split(",") if s.strip()]
        lookup = dict(CAMBRIDGE_STAGES)
        return [lookup.get(k, k) for k in keys]


class TeacherVerificationDocument(db.Model):
    __tablename__ = "teacher_verification_documents"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("teacher_profiles.id"), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class ParentStudentLink(db.Model):
    __tablename__ = "parent_student_links"
    __table_args__ = (db.UniqueConstraint("parent_id", "student_id", name="uq_parent_student_pair"),)

    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    # "pending" | "approved" | "declined"
    status = db.Column(db.String(20), default="pending", nullable=False)
    # Who initiated it — a parent-initiated request needs the student (or a
    # registrar) to approve it before the parent can see anything; a
    # registrar-initiated link is auto-approved, since registrars are
    # trusted school staff maintaining the official roster.
    requested_by = db.Column(db.String(20), nullable=False)  # "parent" | "registrar"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime)

    parent = db.relationship("User", foreign_keys=[parent_id])
    student = db.relationship("User", foreign_keys=[student_id])

