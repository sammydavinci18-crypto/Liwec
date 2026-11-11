import secrets
from datetime import datetime, timedelta

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
    # "client" (default — regular user: requests consultations, enrolls as a
    # student, can be linked as a parent), "registrar" (school-scoped staff:
    # manages enrollment/rosters, does NOT approve teacher applications —
    # that stays with admin), "admin" (the Director: the ONLY person who
    # hosts meetings and consultations, approves every request, and reviews
    # applications). Being a teacher is NOT a role value — it's determined
    # by holding an approved TeacherProfile, so one person can hold several
    # of these at once (e.g. teacher AND parent).
    role = db.Column(db.String(20), default="client", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Moderation — a banned user is force-logged-out and can't log back in
    # (see load_user in app.py and the login check in routes/auth.py).
    is_banned = db.Column(db.Boolean, default=False, nullable=False)
    ban_reason = db.Column(db.Text)  # admin-only note, e.g. for legal/abuse records
    banned_at = db.Column(db.DateTime)

    # LEGACY — no longer used. Every feature is free for everyone now (no
    # tiers). The column is kept only so existing databases, where it is
    # NOT NULL, keep accepting inserts without a migration that drops it.
    is_premium = db.Column(db.Boolean, default=False, nullable=False)

    meetings_hosted = db.relationship(
        "Meeting", backref="host", lazy=True, foreign_keys="Meeting.host_id"
    )
    teacher_profile = db.relationship(
        "TeacherProfile", backref="user", uselist=False, cascade="all, delete-orphan",
        foreign_keys="TeacherProfile.user_id",
    )
    student_profile = db.relationship(
        "StudentProfile", backref="user", uselist=False, cascade="all, delete-orphan",
        foreign_keys="StudentProfile.user_id",
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


MEETING_TYPES = [
    ("consultation", "Consultation"),
    ("class", "Live class"),
    ("meeting", "Meeting"),
]

# How long before the scheduled start invited people may wait in the lobby.
LOBBY_OPENS_MINUTES_BEFORE = 15


class Meeting(db.Model):
    """Every meeting on the platform is created by the admin (directly, or by
    approving a MeetingRequest). Nobody else can create or host one, and only
    people the admin has invited can enter the room."""
    __tablename__ = "meetings"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    room_code = db.Column(db.String(16), unique=True, nullable=False, default=generate_room_code, index=True)
    # Always an admin account. Kept as a plain FK so older rows still load.
    host_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    scheduled_time = db.Column(db.DateTime, nullable=True)
    # scheduled, live, ended, cancelled
    status = db.Column(db.String(20), default="scheduled")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # consultation | class | meeting — see MEETING_TYPES.
    meeting_type = db.Column(db.String(20), default="consultation", nullable=False, server_default="consultation")
    description = db.Column(db.Text)

    # Set when the host actually joins (not when the row is created) — this
    # is what the server-side call-length limit counts from. See sockets.py.
    call_started_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)

    participants = db.relationship("MeetingParticipant", backref="meeting", lazy=True, cascade="all, delete-orphan")
    notes = db.relationship("ConsultationNote", backref="meeting", lazy=True, cascade="all, delete-orphan")
    invites = db.relationship("MeetingInvite", backref="meeting", lazy=True, cascade="all, delete-orphan")

    # ---- access rules (single source of truth: routes AND sockets use these)

    def type_label(self):
        return dict(MEETING_TYPES).get(self.meeting_type, "Meeting")

    def is_hosted_by(self, user):
        return bool(user and user.is_authenticated and user.is_admin and user.id == self.host_id)

    def is_invited(self, user):
        return bool(user and user.is_authenticated and any(i.user_id == user.id for i in self.invites))

    def lobby_opens_at(self):
        if not self.scheduled_time:
            return None
        return self.scheduled_time - timedelta(minutes=LOBBY_OPENS_MINUTES_BEFORE)

    def entry_state(self, user):
        """Why/whether `user` may be in this meeting's room right now:
        "host" | "ok" | "forbidden" | "cancelled" | "not_open".
        Being logged in is never enough — you must be the admin host or have
        been invited by the admin."""
        if self.is_hosted_by(user):
            return "cancelled" if self.status == "cancelled" else "host"
        # Any admin may look back at a finished or cancelled session (recording,
        # notes, details) even if a different admin account originally hosted it.
        # Without this, an admin who isn't the recorded host got a bare 404.
        if user and user.is_authenticated and user.is_admin and self.status in ("ended", "cancelled"):
            return "cancelled" if self.status == "cancelled" else "ok"
        if not self.is_invited(user):
            return "forbidden"
        if self.status == "cancelled":
            return "cancelled"
        if self.status == "ended":
            return "ok"  # may view the recording
        # The admin has already opened the room, so invited guests may come in
        # whatever the schedule says (the waiting-room window only matters
        # before the host has started the session).
        if self.status == "live":
            return "ok"
        opens = self.lobby_opens_at()
        if opens and datetime.utcnow() < opens:
            return "not_open"
        return "ok"


class MeetingInvite(db.Model):
    """The admin's guest list for one meeting. No invite, no entry."""
    __tablename__ = "meeting_invites"
    __table_args__ = (db.UniqueConstraint("meeting_id", "user_id", name="uq_meeting_invite"),)

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    invited_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")


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
# Consultancy is run by the admin only. People don't browse consultants or
# start meetings themselves — they ask, and the admin decides.
# =============================================================================

REQUEST_KINDS = [
    ("education", "Education administration consultation"),
    ("business", "Business consultation"),
    ("meeting", "Meeting request"),
]


class MeetingRequest(db.Model):
    """A person asking the admin for a consultation or a meeting. Nothing
    happens until the admin approves it — approval is what creates the
    Meeting (hosted by the admin, with the requester invited)."""
    __tablename__ = "meeting_requests"

    id = db.Column(db.Integer, primary_key=True)
    requester_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    kind = db.Column(db.String(20), default="education", nullable=False)  # see REQUEST_KINDS
    topic = db.Column(db.String(200), nullable=False)
    details = db.Column(db.Text)
    preferred_time = db.Column(db.DateTime, nullable=True)  # a suggestion only; the admin sets the real time

    # "pending" | "approved" | "declined" | "cancelled"
    status = db.Column(db.String(20), default="pending", nullable=False)
    admin_note = db.Column(db.Text)  # shown to the requester with the decision
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_at = db.Column(db.DateTime)
    decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), nullable=True)

    requester = db.relationship("User", foreign_keys=[requester_id])
    decided_by = db.relationship("User", foreign_keys=[decided_by_id])
    meeting = db.relationship("Meeting", foreign_keys=[meeting_id])

    def kind_label(self):
        return dict(REQUEST_KINDS).get(self.kind, "Request")


# =============================================================================
# Virtual school: teacher verification, and parent-student linking
# =============================================================================

CAMBRIDGE_STAGES = [
    ("primary", "Cambridge Primary"),
    ("lower_secondary", "Cambridge Lower Secondary"),
    ("igcse", "Cambridge IGCSE"),
    ("as_a_level", "Cambridge AS & A Level"),
]


# The academy's three programmes (see the mission document). These are what a
# student applies to. Older applications may still carry a Cambridge-stage key,
# so stage_label() understands both lists.
STUDENT_PROGRAMS = [
    ("bridging", "Year 6–Year 8 Bridging"),
    ("transition", "National Curriculum to Cambridge Transition"),
    ("homeschool", "Home Schooling"),
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

    # "pending" | "approved" | "rejected" — same review flow as teachers.
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



class StudentProfile(db.Model):
    """A student's admission application to the virtual school. Goes through
    the same admin document-review flow as teachers — nobody is
    an enrolled student until an admin approves this."""
    __tablename__ = "student_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    # Shown to the applicant and their family, e.g. "LW-2026-0007". Assigned on
    # application (or lazily for older rows) — see ensure_reference().
    reference_number = db.Column(db.String(20), index=True)

    date_of_birth = db.Column(db.Date)
    desired_stage = db.Column(db.String(30))  # a STUDENT_PROGRAMS key (older rows may hold a Cambridge stage)
    subjects_of_interest = db.Column(db.String(255))
    notes = db.Column(db.Text)  # anything the admin should know (prior schooling, needs, etc.)

    # "pending" | "approved" | "rejected" — same review flow as teachers.
    status = db.Column(db.String(20), default="pending", nullable=False)
    review_note = db.Column(db.Text)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    documents = db.relationship(
        "StudentVerificationDocument", backref="profile", lazy=True, cascade="all, delete-orphan"
    )

    def stage_label(self):
        return dict(STUDENT_PROGRAMS + CAMBRIDGE_STAGES).get(self.desired_stage, self.desired_stage or "—")

    def ensure_reference(self):
        """Give this application its reference number if it has none yet.
        Returns True if it changed (caller should commit)."""
        if self.reference_number or not self.id:
            return False
        year = (self.submitted_at or datetime.utcnow()).year
        self.reference_number = f"LW-{year}-{self.id:04d}"
        return True

    def tracker_stages(self, has_teacher):
        """The four-step admission timeline: a list of
        {"label", "state"} where state is done | current | todo | failed."""
        approved = self.status == "approved"
        rejected = self.status == "rejected"
        return [
            {"label": "Application received", "state": "done"},
            {"label": "Under review", "state": "done" if (approved or rejected) else "current"},
            {"label": "Not approved" if rejected else "Place confirmed",
             "state": "failed" if rejected else ("done" if approved else "todo")},
            {"label": "Placed with a teacher",
             "state": "done" if (approved and has_teacher) else ("current" if approved else "todo")},
        ]


class StudentVerificationDocument(db.Model):
    __tablename__ = "student_verification_documents"

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class StudentTeacherAssignment(db.Model):
    """An admin placing an approved student with an approved teacher. This
    is the ONLY way a student gets connected to a teacher — students can't
    pick their own teacher, by design. Only an admin creates/removes these.
    Later phases (classes/enrollment) will build on this."""
    __tablename__ = "student_teacher_assignments"
    __table_args__ = (db.UniqueConstraint("student_id", "teacher_id", name="uq_student_teacher_pair"),)

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    assigned_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship("User", foreign_keys=[student_id])
    teacher = db.relationship("User", foreign_keys=[teacher_id])
    assigned_by = db.relationship("User", foreign_keys=[assigned_by_id])


# =============================================================================
# School communication & reporting
# =============================================================================

ANNOUNCEMENT_AUDIENCES = [
    ("all", "Everyone"),
    ("students", "Students"),
    ("teachers", "Teachers"),
    ("parents", "Parents & guardians"),
]


class Announcement(db.Model):
    """A notice posted by the admin. With an event_date it also appears on the
    school calendar (term dates, open days, exam weeks...)."""
    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text)
    audience = db.Column(db.String(20), default="all", nullable=False)  # see ANNOUNCEMENT_AUDIENCES
    event_date = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    created_by = db.relationship("User", foreign_keys=[created_by_id])

    def audience_label(self):
        return dict(ANNOUNCEMENT_AUDIENCES).get(self.audience, "Everyone")


class ProgressReport(db.Model):
    """A teacher's written report on one of their placed students. Visible to
    the student, their linked parents, the teacher and the admin."""
    __tablename__ = "progress_reports"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    term = db.Column(db.String(60), nullable=False)       # e.g. "Term 1 2026"
    subject = db.Column(db.String(120), nullable=False)
    grade = db.Column(db.String(30))                       # e.g. "B+", "Meeting expectations"
    summary = db.Column(db.Text, nullable=False)
    strengths = db.Column(db.Text)
    next_steps = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship("User", foreign_keys=[student_id])
    teacher = db.relationship("User", foreign_keys=[teacher_id])
