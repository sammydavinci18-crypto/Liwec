import importlib.util
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


def _postgres_driver(uri: str, have=None):
    """Pick the Postgres driver and the connect options it needs.

    Returns (uri, connect_args). Which driver runs depends on what is installed,
    and psycopg 3 behaves differently from psycopg2: it creates named server-side
    prepared statements. Behind a transaction-mode pooler (Supabase port 6543,
    pgbouncer) different requests share one server connection, so those names
    collide and Postgres answers 'prepared statement "_pg3_0" already exists'.
    Setting prepare_threshold=None turns prepared statements off for psycopg 3.
    psycopg2 does not accept that option, so it is only passed to psycopg 3.
    `have` lets tests say which driver modules exist.
    """
    if not uri:
        return uri, {}
    have = have or (lambda name: importlib.util.find_spec(name) is not None)
    scheme = urlsplit(uri).scheme
    if scheme == "postgresql":                     # no driver named: SQLAlchemy would guess psycopg2
        if not have("psycopg2") and have("psycopg"):
            uri = "postgresql+psycopg" + uri[len("postgresql"):]
            scheme = "postgresql+psycopg"
    if scheme == "postgresql+psycopg":
        return uri, {"prepare_threshold": None}
    return uri, {}


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
        SQLALCHEMY_DATABASE_URI, _PG_CONNECT_ARGS = _postgres_driver(_normalize_postgres_url(RAW_DATABASE_URL))

        # The Supabase pooler (pgbouncer, transaction mode) already pools
        # connections for us, so SQLAlchemy shouldn't keep its own pool on
        # top of it — that just means two layers of pooling fighting each
        # other and stale connections. NullPool opens a fresh connection per
        # checkout and hands it straight to pgbouncer.
        # psycopg2 doesn't use server-side prepared statements, so it is safe
        # behind a transaction-mode pooler as-is. psycopg 3 does, so
        # _postgres_driver() switches them off for it (see its docstring).
        SQLALCHEMY_ENGINE_OPTIONS = {
            "poolclass": NullPool,
            "pool_pre_ping": True,
        }
        if _PG_CONNECT_ARGS:
            SQLALCHEMY_ENGINE_OPTIONS["connect_args"] = _PG_CONNECT_ARGS
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

    # Verification documents (teacher/student ID, certificates). These are
    # sensitive — never served from a public /static path; always through
    # an access-controlled route (see routes/admin.py) that checks the
    # requester is either the owner of the document or an admin.
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
    # Set to "true" for ONE deploy to overwrite the password of the account
    # matching ADMIN_EMAIL with ADMIN_PASSWORD (forgotten-password recovery).
    # Remove it again afterwards.
    ADMIN_RESET = os.environ.get("ADMIN_RESET", "").strip().lower() in ("1", "true", "yes")

    # Supabase Storage — verification documents and recordings live here in
    # production instead of local disk, since local disk doesn't survive a
    # redeploy/restart on most hosting free tiers (Render's free tier has no
    # persistent disk at all). If either is unset, the app transparently
    # falls back to local disk under RECORDINGS_DIR/VERIFICATION_DOCS_DIR
    # (see storage.py) — fine for local development, NOT fine for
    # production on a free/ephemeral host.
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    VERIFICATION_BUCKET = os.environ.get("SUPABASE_VERIFICATION_BUCKET", "verification-docs")
    RECORDINGS_BUCKET = os.environ.get("SUPABASE_RECORDINGS_BUCKET", "recordings")

    # Everything is free and identical for every user — there are no plans
    # or tiers. These are just operating limits the admin can tune.
    #   CALL_LIMIT_MINUTES = 0           -> calls have no time limit
    #   RECORDING_RETENTION_DAYS = 0     -> recordings are kept until deleted
    CALL_LIMIT_MINUTES = int(os.environ.get("CALL_LIMIT_MINUTES", 0))
    RECORDING_RETENTION_DAYS = int(os.environ.get("RECORDING_RETENTION_DAYS", 90))

    # Most people who may be in one lesson at once through the student link
    # (0 = no limit). Calls are peer-to-peer, so every extra camera makes every
    # browser in the room work harder; see README "Class size".
    GUEST_LIMIT = int(os.environ.get("GUEST_LIMIT", 30))

    # Peer-to-peer (no media server) only: once this many people are in a
    # lesson, further students join with their camera off so the teacher's laptop
    # and everyone's phones are not asked to carry another video. Ignored when
    # LiveKit is configured (the media server has no such limit).
    MESH_VIDEO_LIMIT = int(os.environ.get("MESH_VIDEO_LIMIT", 8))

    # NAT-traversal servers the browsers use to find each other for video.
    # Comma-separated. The default is a free public STUN server; for a fully
    # self-hosted setup run your own coturn and list it here, e.g.
    #   ICE_SERVERS=stun:turn.yourschool.org:3478
    # (TURN with credentials: set TURN_USERNAME / TURN_CREDENTIAL too.)
    ICE_SERVERS = [u.strip() for u in os.environ.get("ICE_SERVERS", "stun:stun.l.google.com:19302").split(",") if u.strip()]
    TURN_USERNAME = os.environ.get("TURN_USERNAME", "")
    TURN_CREDENTIAL = os.environ.get("TURN_CREDENTIAL", "")

    # --- TURN relay (needed by students on mobile data / strict school Wi-Fi) ---
    # Pick ONE of these (rtc.py builds the list the browsers get):
    #  1. coturn with a shared secret ("use-auth-secret"): set TURN_URLS and
    #     TURN_SECRET. Each browser receives its own credentials that expire
    #     after TURN_TTL_SECONDS, so nothing reusable is ever exposed.
    #       TURN_URLS=turn:turn.school.org:3478,turn:turn.school.org:3478?transport=tcp,turns:turn.school.org:443?transport=tcp
    #  2. Cloudflare Realtime TURN: set CLOUDFLARE_TURN_KEY_ID and
    #     CLOUDFLARE_TURN_API_TOKEN (short-lived credentials are fetched for you).
    #  3. Any provider giving a fixed username/password: ICE_SERVERS=turn:...
    #     plus TURN_USERNAME / TURN_CREDENTIAL (above).
    TURN_URLS = [u.strip() for u in os.environ.get("TURN_URLS", "").split(",") if u.strip()]
    TURN_SECRET = os.environ.get("TURN_SECRET", "")
    TURN_TTL_SECONDS = int(os.environ.get("TURN_TTL_SECONDS", 6 * 3600))
    CLOUDFLARE_TURN_KEY_ID = os.environ.get("CLOUDFLARE_TURN_KEY_ID", "")
    CLOUDFLARE_TURN_API_TOKEN = os.environ.get("CLOUDFLARE_TURN_API_TOKEN", "")

    # --- LiveKit media server (SFU) ---
    # When these three are set, lessons run through LiveKit instead of the
    # peer-to-peer mesh: every browser sends ONE stream to the server, which
    # forwards the right quality to each viewer. Leave unset to keep the mesh.
    #   LIVEKIT_URL=wss://your-project.livekit.cloud     (what browsers connect to)
    #   LIVEKIT_API_KEY / LIVEKIT_API_SECRET
    # LIVEKIT_HTTP_URL is derived from LIVEKIT_URL (wss->https) unless set.
    LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "").rstrip("/")
    LIVEKIT_HTTP_URL = os.environ.get("LIVEKIT_HTTP_URL", "").rstrip("/")
    LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
    LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")

    # --- Server-side recording (LiveKit Egress) ---
    # The server records the lesson, so a teacher's slow laptop or crashed
    # browser can't ruin it. Egress writes straight into the Supabase recordings
    # bucket through Supabase's S3-compatible endpoint (Supabase dashboard >
    # Storage > S3 Connection: create an access key). It needs:
    #   EGRESS_S3_ENDPOINT   e.g. https://<project>.supabase.co/storage/v1/s3
    #   EGRESS_S3_REGION     e.g. us-east-1 (the project's region)
    #   EGRESS_S3_ACCESS_KEY / EGRESS_S3_SECRET
    #   PUBLIC_BASE_URL      the public https address of THIS app (Egress opens
    #                        a page here to draw the recording layout). Render's
    #                        RENDER_EXTERNAL_URL is used if this is unset.
    # With any of these missing, the teacher's browser records (as before, but
    # hardened: see static/js/recorder.js).
    EGRESS_S3_ENDPOINT = os.environ.get("EGRESS_S3_ENDPOINT", "").rstrip("/")
    EGRESS_S3_REGION = os.environ.get("EGRESS_S3_REGION", "us-east-1")
    EGRESS_S3_ACCESS_KEY = os.environ.get("EGRESS_S3_ACCESS_KEY", "")
    EGRESS_S3_SECRET = os.environ.get("EGRESS_S3_SECRET", "")
    PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")

    # Slide presentations (PowerPoint / PDF the teacher shows the class).
    # Each deck is turned into one picture per slide and kept in the same
    # storage bucket as recordings, under "slides/<deck id>/", until the
    # session ends. PDFs are always supported. PowerPoint files (.pptx/.ppt/
    # .odp) need LibreOffice installed on the server to be turned into a PDF
    # first: it is found automatically, or point SOFFICE_PATH at it. Without
    # it the Slides panel tells the teacher to upload a PDF instead.
    SLIDES_MAX_MB = int(os.environ.get("SLIDES_MAX_MB", 40))
    SLIDES_MAX_PAGES = int(os.environ.get("SLIDES_MAX_PAGES", 150))
    SOFFICE_PATH = os.environ.get("SOFFICE_PATH", "")

    # If a host's connection is gone this long (closed the tab, laptop died) and
    # they have not come back, the session is ended for them and its recording is
    # saved. 0 = never auto-end (the admin must press "Close & save").
    # The recording is a film of the TEACHER'S OWN WINDOW (their browser captures it when the lesson
    # starts). A media-server recorder cannot see that window, so while this is on (default) the
    # teacher's browser records. Set RECORD_TEACHER_WINDOW=0 to let the server recorder film the lesson
    # content (whiteboard, slides, shared screen) instead.
    RECORD_TEACHER_WINDOW = os.environ.get("RECORD_TEACHER_WINDOW", "1").strip().lower() not in ("0", "false", "no", "off")
    HOST_GONE_AUTO_END_MINUTES = int(os.environ.get("HOST_GONE_AUTO_END_MINUTES", 10))

    # Recording is uploaded in ~30s chunks; keep the per-request limit generous.
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB per chunk
