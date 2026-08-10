import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from dotenv import load_dotenv
from sqlalchemy.pool import NullPool

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _normalize_postgres_url(raw_url: str) -> str:
    """Make a Supabase/Postgres URL safe for SQLAlchemy + psycopg2.

    - Rewrites the legacy 'postgres://' scheme to 'postgresql://'
      (SQLAlchemy 1.4+/psycopg2 reject the old scheme outright).
    - Forces sslmode=require if the caller didn't already specify one,
      since Supabase requires TLS.
    """
    if not raw_url:
        return raw_url

    if raw_url.startswith("postgres://"):
        raw_url = "postgresql://" + raw_url[len("postgres://"):]

    parts = urlsplit(raw_url)
    query = dict(parse_qsl(parts.query))
    query.setdefault("sslmode", "require")
    new_query = urlencode(query)

    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


class Config:
    # Flask
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-this-in-production")

    # DB_ENGINE = "sqlite"   (default, zero setup, great for local testing)
    #           = "postgres" (Supabase / any Postgres, incl. pooled connections)
    #           = "mysql"    (legacy option, still supported)
    DB_ENGINE = os.environ.get("DB_ENGINE", "sqlite").lower()

    if DB_ENGINE in ("postgres", "postgresql"):
        # DATABASE_URL is expected to be a Supabase connection string, e.g. the
        # "Transaction" pooler (pgbouncer) string from Supabase > Project
        # Settings > Database > Connection string:
        #
        #   postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
        #
        # Port 6543 = pgbouncer transaction-mode pooler (recommended for a
        # web app on Render, since each request/gunicorn worker can grab a
        # short-lived connection without exhausting Supabase's own Postgres
        # connection limit). Port 5432 = direct/session connection.
        RAW_DATABASE_URL = os.environ.get("DATABASE_URL", "")
        SQLALCHEMY_DATABASE_URI = _normalize_postgres_url(RAW_DATABASE_URL)

        # The Supabase pooler (pgbouncer, transaction mode) already pools
        # connections for us, so SQLAlchemy shouldn't keep its own pool on
        # top of it — that just means two layers of pooling fighting each
        # other and stale connections. NullPool opens a fresh connection per
        # checkout and hands it straight to pgbouncer.
        # psycopg2 (unlike psycopg3/asyncpg) doesn't use server-side prepared
        # statements by default, so it's safe to use as-is against a
        # transaction-mode pgbouncer pooler — no extra connect_args needed.
        SQLALCHEMY_ENGINE_OPTIONS = {
            "poolclass": NullPool,
            "pool_pre_ping": True,
        }
    elif DB_ENGINE == "mysql":
        MYSQL_USER = os.environ.get("MYSQL_USER", "root")
        MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
        MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
        MYSQL_PORT = os.environ.get("MYSQL_PORT", "3306")
        MYSQL_DB = os.environ.get("MYSQL_DB", "consultmeet")

        SQLALCHEMY_DATABASE_URI = (
            f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}"
        )
    else:
        # A single file on disk: consultmeet/instance/consultmeet.db
        # No server, no service, no password to configure.
        SQLITE_PATH = os.environ.get("SQLITE_PATH", str(BASE_DIR / "instance" / "consultmeet.db"))
        Path(SQLITE_PATH).parent.mkdir(parents=True, exist_ok=True)
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{SQLITE_PATH}"

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Where recorded meeting video files are stored. Kept outside static/ so
    # it isn't served directly — access is gated through a login-checked route.
    RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", str(BASE_DIR / "instance" / "recordings"))
    Path(RECORDINGS_DIR).mkdir(parents=True, exist_ok=True)

    # Consultant verification documents (ID, certificates). These are
    # sensitive — never served from a public /static path; always through
    # an access-controlled route (see routes/admin.py) that checks the
    # requester is either the consultant themselves or an admin.
    VERIFICATION_DOCS_DIR = os.environ.get(
        "VERIFICATION_DOCS_DIR", str(BASE_DIR / "instance" / "verification_docs")
    )
    Path(VERIFICATION_DOCS_DIR).mkdir(parents=True, exist_ok=True)

    # Optional: if both are set, the app creates this admin account
    # automatically on boot (see _seed_admin_from_env in app.py). This is
    # for hosting plans without shell/CLI access (e.g. Render's free tier),
    # where `flask make-admin` isn't reachable. Set these in the hosting
    # dashboard's environment variables, not in a committed .env file —
    # and feel free to remove them again once the account exists, since
    # they're only used the first time (never used to overwrite a password
    # you've since changed via /account).
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

    # Recording is uploaded in ~30s chunks; keep the per-request limit generous.
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB per chunk
