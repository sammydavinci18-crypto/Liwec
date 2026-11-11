import sys

from flask import Flask
from flask_login import LoginManager
from datetime import datetime, timezone

from config import Config
from extensions import db, login_manager, socketio, migrate
from models import (
    User, Meeting, MeetingParticipant, MeetingInvite, MeetingRequest, ConsultationNote, Recording,
    Announcement, ProgressReport,
    TeacherProfile, TeacherVerificationDocument, ParentStudentLink,
    StudentProfile, StudentVerificationDocument, StudentTeacherAssignment,
)  # noqa: F401
# ^ every model is imported explicitly (not just User) so db.create_all()
#   below actually knows about every table, not just whichever one
#   happened to be imported elsewhere already.


def _ensure_schema_up_to_date(app):
    """Runs on every boot, before the app serves traffic. Handles three
    situations automatically — no manual `flask db stamp`/shell access
    required, because we've already had one incident (the Python version
    mix-up) caused by a manual step nobody got to run:

      1. A database Alembic already manages (an alembic_version table
         exists) -> just apply any pending migrations. This is the normal
         case for every deploy from here on.
      2. A pre-existing database from before Flask-Migrate was introduced
         (this app's exact situation right now: tables exist via the old
         db.create_all()-only approach, no alembic_version table) -> patch
         known drift (columns added to a model after its table already
         existed, which create_all() can never retrofit), run create_all()
         as a final safety net (harmless/additive for existing tables), then
         stamp the database at the latest migration so Alembic's history
         lines up with reality — automatically, on this exact boot.
      3. A genuinely fresh/empty database -> let Alembic build it from
         scratch via the migration history.
    """
    from pathlib import Path
    from sqlalchemy import inspect, text
    from flask_migrate import upgrade as migrate_upgrade, stamp as migrate_stamp

    migrations_dir = Path(app.root_path) / "migrations" / "versions"
    if not migrations_dir.is_dir():
        # No migration history committed yet in this environment (e.g. this
        # exact process is what's about to create it via `flask db init`/
        # `flask db migrate`). Nothing to sync against — skip cleanly rather
        # than letting Alembic's CLI error (which raises SystemExit, not a
        # normal Exception) blow up app boot.
        return

    with app.app_context():
        # Step 1: let Alembic do its normal job (fresh DB, or pending
        # migrations on a DB it already manages). Failure here must not stop
        # step 2 — that was the cause of a "relation teacher_profiles does
        # not exist" outage: a migration failed quietly, the app booted
        # anyway, and the new tables never got created.
        try:
            table_names = set(inspect(db.engine).get_table_names())
            if "alembic_version" in table_names or "users" not in table_names:
                migrate_upgrade()
        except BaseException as exc:  # Alembic can raise SystemExit
            db.session.rollback()
            print(f"[startup] Alembic upgrade failed, falling back to repair: {exc}", file=sys.stderr)

        # Step 2: repair any drift between the models and the real database.
        try:
            _repair_schema_drift()
            migrate_stamp()  # schema now matches the models == latest migration
            print("[startup] Schema verified against models.", file=sys.stderr)
        except BaseException as exc:
            db.session.rollback()
            print(f"[startup] Schema repair failed (non-fatal): {exc}", file=sys.stderr)


def _repair_schema_drift():
    """Additive only: creates any missing table and adds any missing column
    for every model. Never drops or alters existing data."""
    from sqlalchemy import inspect, text

    engine = db.engine
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # Legacy column that older databases may lack.
    if "users" in existing_tables:
        user_cols = {c["name"] for c in inspector.get_columns("users")}
        if "role" not in user_cols:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'client'"
                ))
            print("[startup] Patched missing users.role column.", file=sys.stderr)

    # 1) Missing tables
    db.create_all()

    # 2) Missing columns on tables that already existed
    dialect = engine.dialect
    quote = dialect.identifier_preparer.quote
    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # just created in full by create_all()
        have = {c["name"] for c in inspect(engine).get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = f"ALTER TABLE {quote(table.name)} ADD COLUMN {quote(col.name)} {col.type.compile(dialect=dialect)}"
            if col.server_default is not None and hasattr(col.server_default, "arg"):
                arg = col.server_default.arg
                ddl += f" DEFAULT {arg if not isinstance(arg, str) else repr(arg)}"
                if not col.nullable:
                    ddl += " NOT NULL"
            with engine.begin() as conn:
                conn.execute(text(ddl))
            print(f"[startup] Added missing column {table.name}.{col.name}.", file=sys.stderr)


def _seed_admin_from_env(app):
    """Creates (or promotes) an admin account from ADMIN_EMAIL/ADMIN_PASSWORD
    env vars, if both are set. Built for hosting plans without shell access
    (e.g. Render's free tier) where `flask make-admin` isn't reachable.

    Deliberately only sets the password when *creating* the account. If the
    account already exists, this only makes sure its role is "admin" — it
    never overwrites a password, so redeploying doesn't undo a password
    you've since changed via /account.
    """
    email = (app.config.get("ADMIN_EMAIL") or "").strip().lower()
    password = app.config.get("ADMIN_PASSWORD") or ""
    if not email or not password:
        return

    with app.app_context():
        try:
            user = User.query.filter_by(email=email).first()
            if user is None:
                user = User(name="Admin", email=email, role="admin")
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                print(f"[startup] Seeded admin account for {email}.", file=sys.stderr)
            else:
                changed = False
                if user.role != "admin":
                    user.role = "admin"
                    changed = True
                    print(f"[startup] Promoted existing user {email} to admin.", file=sys.stderr)
                # Recovery switch: only when ADMIN_RESET=true is set.
                if app.config.get("ADMIN_RESET"):
                    user.set_password(password)
                    changed = True
                    print(f"[startup] Reset password for admin {email} (ADMIN_RESET is on — remove it now).", file=sys.stderr)
                if changed:
                    db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"[startup] Admin seed skipped/failed (non-fatal): {exc}", file=sys.stderr)


def _cleanup_expired_recordings(app):
    import storage
    with app.app_context():
        now = datetime.utcnow()
        expired = Recording.query.filter(
            Recording.expires_at.isnot(None),
            Recording.expires_at <= now,
            Recording.deleted_at.is_(None),
        ).all()
        if not expired:
            return
        bucket = app.config["RECORDINGS_BUCKET"]
        for rec in expired:
            ok = storage.delete_object(bucket, rec.filename)
            rec.deleted_at = datetime.utcnow()
            print(f"[cleanup] Expired recording {rec.id} (meeting {rec.meeting_id}) removed: {ok}", file=sys.stderr)
        db.session.commit()


def _start_recording_cleanup_thread(app, interval_seconds=3600):
    """Runs the retention sweep periodically in a background thread. Single
    worker process (-w 1 in the Procfile), so this only ever runs once —
    no risk of two workers double-processing the same recordings.

    Skips starting a second copy under Flask's debug reloader, which
    re-imports this module in a parent watcher process too.
    """
    import os
    import threading
    import time

    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    def loop():
        while True:
            time.sleep(interval_seconds)
            try:
                _cleanup_expired_recordings(app)
            except Exception as exc:
                print(f"[cleanup] Recording cleanup failed (non-fatal): {exc}", file=sys.stderr)

    threading.Thread(target=loop, daemon=True).start()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    socketio.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        user = User.query.get(int(user_id))
        # Returning None here is how Flask-Login treats "not logged in" —
        # so a banned user is force-logged-out on their very next request,
        # not just blocked from logging in again.
        if user is not None and user.is_banned:
            return None
        return user

    @app.context_processor
    def inject_current_year():
        return {"current_year": datetime.now(timezone.utc).year}

    @app.context_processor
    def inject_admin_counts():
        """Lets the admin's nav and control room show what is waiting."""
        from flask_login import current_user
        from models import StudentProfile, TeacherProfile, StudentTeacherAssignment
        counts = {"pending_requests_count": 0, "pending_students_count": 0,
                  "pending_teachers_count": 0, "unplaced_count": 0, "attention_total": 0}
        try:
            if current_user.is_authenticated and current_user.is_admin:
                placed = db.session.query(StudentTeacherAssignment.student_id)
                counts["pending_requests_count"] = MeetingRequest.query.filter_by(status="pending").count()
                counts["pending_students_count"] = StudentProfile.query.filter_by(status="pending").count()
                counts["pending_teachers_count"] = TeacherProfile.query.filter_by(status="pending").count()
                counts["unplaced_count"] = (
                    StudentProfile.query.filter_by(status="approved")
                    .filter(~StudentProfile.user_id.in_(placed)).count()
                )
                counts["attention_total"] = sum(counts.values())
        except Exception:
            db.session.rollback()
        return counts

    @app.template_filter("dt")
    def format_datetime(value):
        """Portable date/time display (the platform stores and shows UTC)."""
        if not value:
            return "—"
        return value.strftime("%a %d %b %Y, %H:%M") + " UTC"

    from routes.auth import auth_bp
    from routes.main import main_bp
    from routes.meetings import meetings_bp
    from routes.consultation import consult_bp
    from routes.portal import portal_bp
    from routes.pages import pages_bp
    from routes.admin import admin_bp
    from routes.teachers import teachers_bp
    from routes.family import family_bp
    from routes.students import students_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(consult_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(teachers_bp)
    app.register_blueprint(family_bp)
    app.register_blueprint(students_bp)

    # Registers the Socket.IO event handlers defined in sockets.py
    import sockets  # noqa: F401

    # Keeps the schema in sync on every boot — see _ensure_schema_up_to_date
    # above for what this actually does depending on the database's state.
    # This runs regardless of whether preDeployCommand ran (e.g. if this was
    # ever deployed as a manually created Render service instead of via the
    # render.yaml Blueprint) — so a plain deploy self-heals either way.
    with app.app_context():
        try:
            _ensure_schema_up_to_date(app)
        except Exception as exc:  # e.g. DB briefly unreachable during a cold start
            print(f"[startup] Schema sync failed — could not reach the database: {exc}", file=sys.stderr)

    _seed_admin_from_env(app)
    _start_recording_cleanup_thread(app)

    @app.cli.command("init-db")
    def init_db():
        """Legacy/dev convenience only — prefer `flask db upgrade`.
        Creates any missing tables directly (no migration history), which is
        fine for a quick local sqlite sandbox but should NOT be used against
        a database that Flask-Migrate manages (i.e. production)."""
        with app.app_context():
            db.create_all()
        print("Database tables created.")

    @app.cli.command("make-admin")
    def make_admin():
        """Promote a user to admin (the Director). Admins are the only people who
        can host meetings, approve requests and review applications. Run with: flask --app app make-admin <email>
        There's deliberately no web route that can do this — admin access is
        only ever granted from the server/CLI side."""
        import click
        email = click.prompt("Email of the user to promote")
        user = User.query.filter_by(email=email.strip().lower()).first()
        if not user:
            print(f"No user found with email {email}")
            return
        user.role = "admin"
        db.session.commit()
        print(f"{user.name} ({user.email}) is now an admin.")

    @app.cli.command("reset-admin-password")
    def reset_admin_password():
        """Set a new password: flask --app app reset-admin-password"""
        import click
        email = click.prompt("Email of the account")
        user = User.query.filter_by(email=email.strip().lower()).first()
        if not user:
            print(f"No user found with email {email}")
            return
        user.set_password(click.prompt("New password", hide_input=True, confirmation_prompt=True))
        user.role = "admin"
        db.session.commit()
        print(f"Password reset; {user.email} is an admin.")

    return app


app = create_app()

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
