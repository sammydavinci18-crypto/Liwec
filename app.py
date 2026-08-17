import sys

from flask import Flask
from flask_login import LoginManager
from datetime import datetime, timezone

from config import Config
from extensions import db, login_manager, socketio, migrate
from models import (
    User, Meeting, MeetingParticipant, ConsultationNote, Recording,
    ConsultantProfile, VerificationDocument, AvailabilitySlot, Appointment,
    Conversation, DirectMessage,
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
        try:
            inspector = inspect(db.engine)
            table_names = set(inspector.get_table_names())

            if "alembic_version" in table_names:
                migrate_upgrade()
                return

            if "users" in table_names:
                existing_columns = {c["name"] for c in inspector.get_columns("users")}
                if "role" not in existing_columns:
                    db.session.execute(text(
                        "ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'client'"
                    ))
                    db.session.commit()
                    print("[startup] Patched missing users.role column.", file=sys.stderr)
                db.create_all()
                migrate_stamp()
                print("[startup] Adopted Flask-Migrate on an existing database (stamped at head).", file=sys.stderr)
            else:
                migrate_upgrade()
        except Exception as exc:
            db.session.rollback()
            print(f"[startup] Schema sync skipped/failed (non-fatal): {exc}", file=sys.stderr)


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
            elif user.role != "admin":
                user.role = "admin"
                db.session.commit()
                print(f"[startup] Promoted existing user {email} to admin.", file=sys.stderr)
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

    from routes.auth import auth_bp
    from routes.main import main_bp
    from routes.meetings import meetings_bp
    from routes.consultants import consultants_bp
    from routes.appointments import appointments_bp
    from routes.messages import messages_bp
    from routes.admin import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(consultants_bp)
    app.register_blueprint(appointments_bp)
    app.register_blueprint(messages_bp)
    app.register_blueprint(admin_bp)

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
        """Promote a user to admin so they can review consultant verification
        requests. Run with: flask --app app make-admin <email>
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

    return app


app = create_app()

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
