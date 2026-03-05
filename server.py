import hashlib
import hmac
import json as _json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "WARNING").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import sessionmaker
from starlette.middleware.sessions import SessionMiddleware

# ══════════════════════════════════════════════════════════════════════
#  ENV + STARTUP
# ══════════════════════════════════════════════════════════════════════

load_dotenv()

app = FastAPI()

# ── CORS ──────────────────────────────────────────────────────────────
cors_origins_env = os.getenv("CORS_ORIGINS")
if not cors_origins_env:
    raise ValueError("CORS_ORIGINS environment variable must be set")
cors_origins = cors_origins_env.split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Session middleware ────────────────────────────────────────────────
SESSION_SECRET = os.getenv("SESSION_SECRET_KEY")
if not SESSION_SECRET:
    raise ValueError("SESSION_SECRET_KEY environment variable must be set")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# ── CSRF token helpers ────────────────────────────────────────────────
_CSRF_TOKEN_FIELD = "_csrf_token"
_CSRF_TOKEN_LENGTH = 32


def _generate_csrf_token(request: Request) -> str:
    """Generate or retrieve a CSRF token stored in the session."""
    token = request.session.get(_CSRF_TOKEN_FIELD)
    if not token:
        token = secrets.token_urlsafe(_CSRF_TOKEN_LENGTH)
        request.session[_CSRF_TOKEN_FIELD] = token
    return token


def _validate_csrf_token(request: Request, form_token: str) -> bool:
    """Validate CSRF token from form against session."""
    session_token = request.session.get(_CSRF_TOKEN_FIELD)
    if not session_token or not form_token:
        return False
    return hmac.compare_digest(session_token, form_token)


def _consume_csrf_token(request: Request):
    """Rotate CSRF token after successful validation."""
    request.session[_CSRF_TOKEN_FIELD] = secrets.token_urlsafe(_CSRF_TOKEN_LENGTH)


# ── Default credentials ──────────────────────────────────────────────
DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME")
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD")
if not DEFAULT_ADMIN_USERNAME or not DEFAULT_ADMIN_PASSWORD:
    raise ValueError(
        "DEFAULT_ADMIN_USERNAME and DEFAULT_ADMIN_PASSWORD environment variables must be set"
    )

DEFAULT_USER_USERNAME = os.getenv("DEFAULT_USER_USERNAME")
DEFAULT_USER_PASSWORD = os.getenv("DEFAULT_USER_PASSWORD")
if not DEFAULT_USER_USERNAME or not DEFAULT_USER_PASSWORD:
    raise ValueError(
        "DEFAULT_USER_USERNAME and DEFAULT_USER_PASSWORD environment variables must be set"
    )

# ── Response signing key (anti-tamper) ────────────────────────────────
RESPONSE_SIGN_KEY = os.getenv("RESPONSE_SIGN_KEY")
if not RESPONSE_SIGN_KEY:
    raise ValueError(
        "RESPONSE_SIGN_KEY environment variable must be set. "
        'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
    )

# ── Paths ─────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# ══════════════════════════════════════════════════════════════════════
#  STATIC FILES
# ══════════════════════════════════════════════════════════════════════


@app.get("/static/css/{file_path:path}")
async def serve_css(file_path: str):
    file_location = STATIC_DIR / "css" / file_path
    if file_location.exists() and file_location.is_file():
        return FileResponse(file_location, media_type="text/css")
    raise HTTPException(status_code=404, detail="CSS file not found")


@app.get("/static/js/{file_path:path}")
async def serve_js(file_path: str):
    file_location = STATIC_DIR / "js" / file_path
    if file_location.exists() and file_location.is_file():
        return FileResponse(file_location, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="JS file not found")


@app.get("/static/img/{file_path:path}")
async def serve_img(file_path: str):
    file_location = STATIC_DIR / "img" / file_path
    if file_location.exists() and file_location.is_file():
        ext = file_path.lower().split(".")[-1]
        media_types = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "svg": "image/svg+xml",
            "ico": "image/x-icon",
            "webp": "image/webp",
        }
        media_type = media_types.get(ext, "application/octet-stream")
        return FileResponse(file_location, media_type=media_type)
    raise HTTPException(status_code=404, detail="Image file not found")


@app.get("/static/{file_path:path}")
async def serve_static(file_path: str):
    file_location = STATIC_DIR / file_path
    if file_location.exists() and file_location.is_file():
        return FileResponse(file_location)
    raise HTTPException(status_code=404, detail="Static file not found")


try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass

templates = Jinja2Templates(directory="templates")


def _fmt_datetime(value, fmt="%d %b %Y %H:%M"):
    """Jinja2 filter: format a datetime object."""
    if value is None:
        return "--"
    try:
        return value.strftime(fmt)
    except Exception:
        return "--"


def _fmt_date(value, fmt="%d %b %Y"):
    """Jinja2 filter: format a date object (no time)."""
    if value is None:
        return "--"
    try:
        return value.strftime(fmt)
    except Exception:
        return "--"


templates.env.filters["fmtdt"] = _fmt_datetime
templates.env.filters["fmtd"] = _fmt_date

# ══════════════════════════════════════════════════════════════════════
#  PASSWORD HASHING — pbkdf2_hmac (no external deps)
# ══════════════════════════════════════════════════════════════════════

_HASH_ALGO = "sha256"
_HASH_ITERATIONS = 260_000  # OWASP recommendation for PBKDF2-SHA256
_SALT_BYTES = 16
_HASH_PREFIX = "$pbkdf2$"


def hash_password(plain: str) -> str:
    """Hash a plaintext password -> '$pbkdf2$<salt_hex>$<hash_hex>'"""
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(_HASH_ALGO, plain.encode(), salt, _HASH_ITERATIONS)
    return f"{_HASH_PREFIX}{salt.hex()}${dk.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """
    Verify a password against stored value.
    Supports BOTH hashed ($pbkdf2$...) and legacy plaintext for migration.
    """
    if stored.startswith(_HASH_PREFIX):
        # hashed — parse and verify
        parts = stored[len(_HASH_PREFIX) :].split("$", 1)
        if len(parts) != 2:
            return False
        salt_hex, hash_hex = parts
        try:
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
        except ValueError:
            return False
        dk = hashlib.pbkdf2_hmac(_HASH_ALGO, plain.encode(), salt, _HASH_ITERATIONS)
        return hmac.compare_digest(dk, expected)
    else:
        # legacy plaintext — constant-time compare
        return hmac.compare_digest(stored.encode(), plain.encode())


def validate_password_length(password: str) -> bool:
    return len(password) >= 6


# ══════════════════════════════════════════════════════════════════════
#  HMAC RESPONSE SIGNING — prevents MITM response tampering
# ══════════════════════════════════════════════════════════════════════
#
#  How it works:
#    1. Client sends a random  nonce  with the login request.
#    2. Server computes:
#         payload  = f"{nonce}:{username}:{session_token}:{timestamp}"
#         sig      = HMAC-SHA256(RESPONSE_SIGN_KEY, payload)
#    3. Server returns  nonce, ts, sig  alongside the normal response.
#    4. Client re-computes the HMAC with its embedded key and verifies:
#         • nonce matches what it sent   (no replay of old responses)
#         • ts within ±30 s of now        (no delayed replay)
#         • sig matches                   (no body tampering)
#
#  Even if an attacker proxies the TLS traffic, they cannot forge sig
#  without knowing RESPONSE_SIGN_KEY.

_SIG_MAX_AGE_SECONDS = 30


def _sign_response(nonce: str, username: str, session_token: str) -> Tuple[int, str]:
    """Return (timestamp, hex_signature)."""
    ts = int(time.time())
    payload = f"{nonce}:{username}:{session_token}:{ts}"
    sig = hmac.new(
        RESPONSE_SIGN_KEY.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return ts, sig


# ══════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set.")

database_url = DATABASE_URL
if database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

log.info("Connecting to database...")
try:
    engine = create_engine(database_url)
    log.info("Database connection successful")
except Exception as e:
    log.error("Failed to connect to database: %s", e)
    raise

metadata = MetaData()

# ── users table (enhanced) ────────────────────────────────────────────
users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(50), unique=True, nullable=False),
    Column("password", String(256), nullable=False),  # now stores hash
    Column("role", String(10), nullable=False),
    Column("created_at", DateTime, nullable=True),
    Column("updated_at", DateTime, nullable=True),
    Column("last_login_at", DateTime, nullable=True),
    Column("last_login_ip", String(45), nullable=True),
    Column("login_count", Integer, nullable=True, default=0),
    Column("is_active", Boolean, nullable=True, default=True),
)

copied_text_history = Table(
    "copied_text_history",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(50), nullable=False),
    Column("text", String, nullable=False),
)

submitted_text_history = Table(
    "submitted_text_history",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(50), nullable=False),
    Column("text", String, nullable=False),
)

clipboard_updates = Table(
    "clipboard_updates",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(50), nullable=False),
    Column("text", String, nullable=False),
)

# ── login sessions (device tracking + single-session) ─────────────────
login_sessions = Table(
    "login_sessions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(50), nullable=False),
    Column("session_token", String(128), unique=True, nullable=False),
    Column("ip_address", String(45), nullable=True),
    Column("device_os", String(100), nullable=True),
    Column("device_name", String(100), nullable=True),
    Column("hostname", String(100), nullable=True),
    Column("mac_address", String(50), nullable=True),
    Column("screen_resolution", String(30), nullable=True),
    Column("python_version", String(30), nullable=True),
    Column("app_version", String(30), nullable=True),
    Column("logged_in_at", DateTime, nullable=False),
    Column("last_active_at", DateTime, nullable=False),
    Column("is_active", Boolean, nullable=False, default=True),
)

# ── login attempts (rate limiting) ────────────────────────────────────
login_attempts = Table(
    "login_attempts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("identifier", String(100), nullable=False),
    Column("attempted_at", DateTime, nullable=False),
    Column("was_successful", Boolean, nullable=False, default=False),
)

# ── Rate-limit settings ──────────────────────────────────────────────
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW_MINUTES = 5

# ── Single-session login cooldown (seconds) ──────────────────────────
LOGIN_COOLDOWN_SECONDS = int(os.getenv("LOGIN_COOLDOWN_SECONDS", "5"))


def _check_login_cooldown(db, username: str) -> tuple[bool, int]:
    """
    Check if the user logged in too recently.
    Returns (is_blocked, seconds_remaining).
    Prevents race-condition double-logins from multiple devices.
    """
    row = db.execute(
        login_sessions.select()
        .where(login_sessions.c.username == username)
        .where(login_sessions.c.is_active == True)  # noqa: E712
        .order_by(login_sessions.c.logged_in_at.desc())
    ).fetchone()

    if not row:
        return False, 0

    now = datetime.now(timezone.utc)
    last_login = row.logged_in_at
    if last_login.tzinfo is None:
        last_login = last_login.replace(tzinfo=timezone.utc)

    elapsed = (now - last_login).total_seconds()
    if elapsed < LOGIN_COOLDOWN_SECONDS:
        remaining = int(LOGIN_COOLDOWN_SECONDS - elapsed) + 1
        return True, remaining

    return False, 0


# ══════════════════════════════════════════════════════════════════════
#  DB MIGRATION — safely add new columns to existing tables
# ══════════════════════════════════════════════════════════════════════

_USERS_NEW_COLUMNS = {
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP",
    "last_login_at": "TIMESTAMP",
    "last_login_ip": "VARCHAR(45)",
    "login_count": "INTEGER DEFAULT 0",
    "is_active": "BOOLEAN DEFAULT TRUE",
}


def _migrate_users_table(conn, inspector):
    """Add any missing columns to the users table."""
    existing = {col["name"] for col in inspector.get_columns("users")}

    # Remove legacy secret_key if present
    if "secret_key" in existing:
        log.info("  -> Dropping legacy secret_key column")
        conn.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS secret_key;"))
        conn.commit()

    # Widen password column from VARCHAR(50) to VARCHAR(256) for hashes
    # PostgreSQL: safe to do, data is preserved
    try:
        conn.execute(text("ALTER TABLE users ALTER COLUMN password TYPE VARCHAR(256);"))
        conn.commit()
        log.info("  -> Widened password column to VARCHAR(256)")
    except Exception as e:
        log.debug("  -> password column resize skipped: %s", e)
        conn.rollback()

    # Add new columns
    for col_name, col_type in _USERS_NEW_COLUMNS.items():
        if col_name not in existing:
            log.info("  -> Adding column users.%s", col_name)
            try:
                conn.execute(
                    text(f"ALTER TABLE users ADD COLUMN {col_name} {col_type};")
                )
                conn.commit()
            except Exception as e:
                log.debug("    (skipped: %s)", e)
                conn.rollback()


def _migrate_plaintext_passwords(conn):
    """Hash any plaintext passwords still in the DB."""
    rows = conn.execute(text("SELECT id, password FROM users")).fetchall()
    migrated = 0
    for row in rows:
        if not row.password.startswith(_HASH_PREFIX):
            hashed = hash_password(row.password)
            conn.execute(
                text("UPDATE users SET password = :pw WHERE id = :uid"),
                {"pw": hashed, "uid": row.id},
            )
            migrated += 1
    if migrated:
        conn.commit()
        log.info("  -> Hashed %d plaintext password(s)", migrated)


try:
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()

    with engine.connect() as conn:
        if "users" in existing_tables:
            log.info("Migrating users table...")
            _migrate_users_table(conn, inspector)

        # Create any brand-new tables
        metadata.create_all(engine)
        log.info("Tables created / updated successfully")

        if "users" in existing_tables or "users" in inspector.get_table_names():
            log.info("Checking for plaintext passwords...")
            _migrate_plaintext_passwords(conn)

except Exception as e:
    log.error("Error during DB setup: %s", e)
    raise

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# ══════════════════════════════════════════════════════════════════════
#  STARTUP — create default users
# ══════════════════════════════════════════════════════════════════════


@app.on_event("startup")
async def startup_event():
    db = SessionLocal()
    try:
        log.info("Initializing default users...")

        if not validate_password_length(DEFAULT_ADMIN_PASSWORD):
            raise ValueError("DEFAULT_ADMIN_PASSWORD must be >= 6 characters")
        if not validate_password_length(DEFAULT_USER_PASSWORD):
            raise ValueError("DEFAULT_USER_PASSWORD must be >= 6 characters")

        now = datetime.now(timezone.utc)

        # Default admin
        existing_admin = db.execute(
            users.select().where(users.c.username == DEFAULT_ADMIN_USERNAME)
        ).fetchone()
        if not existing_admin:
            db.execute(
                users.insert().values(
                    username=DEFAULT_ADMIN_USERNAME,
                    password=hash_password(DEFAULT_ADMIN_PASSWORD),
                    role="admin",
                    created_at=now,
                    updated_at=now,
                    login_count=0,
                    is_active=True,
                )
            )
            log.info("  Created admin: %s", DEFAULT_ADMIN_USERNAME)
        else:
            log.debug("  Admin '%s' already exists", DEFAULT_ADMIN_USERNAME)

        # Default user
        existing_user = db.execute(
            users.select().where(users.c.username == DEFAULT_USER_USERNAME)
        ).fetchone()
        if not existing_user:
            db.execute(
                users.insert().values(
                    username=DEFAULT_USER_USERNAME,
                    password=hash_password(DEFAULT_USER_PASSWORD),
                    role="user",
                    created_at=now,
                    updated_at=now,
                    login_count=0,
                    is_active=True,
                )
            )
            log.info("  Created user: %s", DEFAULT_USER_USERNAME)
        else:
            log.debug("  User '%s' already exists", DEFAULT_USER_USERNAME)

        db.commit()

        all_users = db.execute(users.select()).fetchall()
        log.debug("Users in DB:")
        for u in all_users:
            active = getattr(u, "is_active", True)
            count = getattr(u, "login_count", 0) or 0
            log.debug(
                "  ID=%s  user=%s  role=%s  active=%s  logins=%s",
                u.id,
                u.username,
                u.role,
                active,
                count,
            )
    except Exception as e:
        log.error("Startup error: %s", e)
        raise
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════


# Set of trusted proxy IPs — only these may set X-Forwarded-For.
# In production behind Vercel/Cloudflare, the LAST entry in X-Forwarded-For
# (added by the trusted edge proxy) is the real client IP.
# For safety, we only trust the header when the direct peer is a known proxy.
_TRUSTED_PROXIES = set(filter(None, os.getenv("TRUSTED_PROXY_IPS", "").split(",")))


def _get_client_ip(request: Request) -> str:
    """
    Get client IP safely.
    Only trusts X-Forwarded-For / X-Real-IP when the direct connection
    comes from a known proxy.  Otherwise, uses the socket peer address
    to prevent attackers from spoofing their IP to bypass rate limits.
    """
    peer_ip = request.client.host if request.client else "unknown"

    # Only trust forwarded headers if the direct peer is a trusted proxy
    if peer_ip in _TRUSTED_PROXIES or _TRUSTED_PROXIES == {""}:
        # Vercel/Cloudflare: first entry is the real client
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()

    return peer_ip


def _check_rate_limit(db, identifier: str) -> Tuple[bool, int]:
    window_start = datetime.now(timezone.utc) - timedelta(
        minutes=RATE_LIMIT_WINDOW_MINUTES
    )
    failed = db.execute(
        login_attempts.select()
        .where(login_attempts.c.identifier == identifier)
        .where(login_attempts.c.was_successful == False)  # noqa: E712
        .where(login_attempts.c.attempted_at >= window_start)
        .order_by(login_attempts.c.attempted_at.desc())
    ).fetchall()

    if len(failed) >= RATE_LIMIT_MAX_ATTEMPTS:
        oldest = failed[-1].attempted_at
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        unlock_at = oldest + timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
        remaining = (unlock_at - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            return True, int(remaining)
    return False, 0


def _record_attempt(db, identifier: str, success: bool):
    db.execute(
        login_attempts.insert().values(
            identifier=identifier,
            attempted_at=datetime.now(timezone.utc),
            was_successful=success,
        )
    )
    db.commit()


def _sanitize_username(name: str) -> str:
    """Strip all leading/trailing whitespace (incl. tabs, newlines) from a username."""
    if not name:
        return name
    return name.strip()


def _is_user_banned(db, username: str) -> bool:
    """Return True if the user exists and is_active is False (banned)."""
    row = db.execute(users.select().where(users.c.username == username)).fetchone()
    if not row:
        return False
    return getattr(row, "is_active", True) is False


def _find_user_by_credentials(
    db, username: str, password: str, role: str | None = None
):
    """
    Find a user by username + password.  Returns the row or None.
    If role is given, also filters by role.
    Returns None for wrong credentials — callers should check
    _is_user_banned() separately for a specific banned message.
    """
    query = users.select().where(users.c.username == username)
    if role:
        query = query.where(users.c.role == role)
    u = db.execute(query).fetchone()
    if not u:
        return None
    # Check active
    is_active = getattr(u, "is_active", True)
    if is_active is False:
        return None
    # Verify password
    if not verify_password(password, u.password):
        return None
    # Auto-upgrade plaintext password to hash
    if not u.password.startswith(_HASH_PREFIX):
        try:
            db.execute(
                users.update()
                .where(users.c.id == u.id)
                .values(password=hash_password(password))
            )
            db.commit()
            log.info("  -> Auto-hashed password for user %s", u.username)
        except Exception:
            pass
    return u


def _update_user_login_stats(db, user_id: int, ip: str):
    """Bump login_count, last_login_at, last_login_ip."""
    now = datetime.now(timezone.utc)
    try:
        db.execute(
            users.update()
            .where(users.c.id == user_id)
            .values(
                last_login_at=now,
                last_login_ip=ip,
                login_count=text("COALESCE(login_count, 0) + 1"),
                updated_at=now,
            )
        )
        db.commit()
    except Exception as e:
        log.error("  login stats update failed: %s", e)


# ── Pydantic models ──────────────────────────────────────────────────
class HistoryItem(BaseModel):
    text: str


class DeviceInfo(BaseModel):
    device_os: Optional[str] = None
    device_name: Optional[str] = None
    hostname: Optional[str] = None
    mac_address: Optional[str] = None
    screen_resolution: Optional[str] = None
    python_version: Optional[str] = None
    app_version: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════
#  WEB ROUTES — admin + user dashboards (HTML)
# ══════════════════════════════════════════════════════════════════════


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Admin ─────────────────────────────────────────────────────────────


@app.get("/admin", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        "admin_login.html", {"request": request, "error": error}
    )


@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()

    if not validate_password_length(password):
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Invalid credentials"},
        )

    client_ip = _get_client_ip(request)
    db = SessionLocal()
    try:
        # ── rate limit ────────────────────────────────────────────
        is_locked, seconds_left = _check_rate_limit(db, f"admin:{client_ip}")
        if is_locked:
            mins = (seconds_left // 60) + (1 if seconds_left % 60 else 0)
            return templates.TemplateResponse(
                "admin_login.html",
                {
                    "request": request,
                    "error": f"Too many failed attempts. Try again in {mins} min.",
                },
            )

        # ── banned check ──────────────────────────────────────────
        if _is_user_banned(db, username):
            return templates.TemplateResponse(
                "admin_login.html",
                {"request": request, "error": "Your account has been banned."},
            )

        user = _find_user_by_credentials(db, username, password, role="admin")

        if user:
            _record_attempt(db, f"admin:{client_ip}", True)
            _update_user_login_stats(db, user.id, client_ip)
            request.session["user"] = {"username": username, "role": "admin"}
            return _admin_dash_response(request, db)
        else:
            _record_attempt(db, f"admin:{client_ip}", False)
            return templates.TemplateResponse(
                "admin_login.html",
                {"request": request, "error": "Invalid credentials"},
            )
    except Exception as e:
        log.error("Admin login error: %s", e)
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Server error during login"},
        )
    finally:
        db.close()


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if request.session.get("user", {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        return _admin_dash_response(request, db)
    finally:
        db.close()


@app.get("/admin/api/user_history/{username}")
async def admin_get_user_history(username: str, request: Request):
    """Admin-only endpoint: get a user's copied AND submitted text history."""
    if request.session.get("user", {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        copied_items = db.execute(
            copied_text_history.select()
            .where(copied_text_history.c.username == username)
            .order_by(copied_text_history.c.id.desc())
        ).fetchall()
        submitted_items = db.execute(
            submitted_text_history.select()
            .where(submitted_text_history.c.username == username)
            .order_by(submitted_text_history.c.id.desc())
        ).fetchall()
        return JSONResponse(
            content={
                "status": "success",
                "username": username,
                "copied_text_history": [
                    {"id": i.id, "text": i.text} for i in copied_items
                ],
                "submitted_text_history": [
                    {"id": i.id, "text": i.text} for i in submitted_items
                ],
            }
        )
    except Exception as e:
        log.error("Admin: error fetching history for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error fetching history"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/admin/api/clear_user_history/{username}")
async def admin_clear_user_history(username: str, request: Request):
    """Admin-only endpoint: clear a user's copied text history."""
    if request.session.get("user", {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            copied_text_history.delete().where(
                copied_text_history.c.username == username
            )
        )
        db.commit()
        return JSONResponse(
            content={
                "status": "success",
                "message": f"Cleared history for {username}",
            }
        )
    except Exception as e:
        log.error("Admin: error clearing history for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error clearing history"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/admin/api/delete_user_history_item/{username}")
async def admin_delete_user_history_item(
    username: str, item: HistoryItem, request: Request
):
    """Admin-only endpoint: delete a single copied text entry for a user."""
    if request.session.get("user", {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            copied_text_history.delete()
            .where(copied_text_history.c.username == username)
            .where(copied_text_history.c.text == item.text)
        )
        db.commit()
        return JSONResponse(content={"status": "success", "message": "Item deleted"})
    except Exception as e:
        log.error("Admin: error deleting history item for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error deleting item"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/admin/api/delete_user_submitted_item/{username}")
async def admin_delete_user_submitted_item(
    username: str, item: HistoryItem, request: Request
):
    """Admin-only endpoint: delete a single submitted text entry for a user."""
    if request.session.get("user", {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            submitted_text_history.delete()
            .where(submitted_text_history.c.username == username)
            .where(submitted_text_history.c.text == item.text)
        )
        db.commit()
        return JSONResponse(
            content={"status": "success", "message": "Submitted item deleted"}
        )
    except Exception as e:
        log.error("Admin: error deleting submitted item for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error deleting submitted item"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/admin/api/clear_user_submitted/{username}")
async def admin_clear_user_submitted(username: str, request: Request):
    """Admin-only endpoint: clear a user's submitted text history."""
    if request.session.get("user", {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            submitted_text_history.delete().where(
                submitted_text_history.c.username == username
            )
        )
        db.commit()
        return JSONResponse(
            content={
                "status": "success",
                "message": f"Cleared submitted history for {username}",
            }
        )
    except Exception as e:
        log.error("Admin: error clearing submitted history for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error clearing submitted history"},
            status_code=500,
        )
    finally:
        db.close()


@app.get("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/add_user")
async def add_user(request: Request):
    if request.session.get("user", {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    form = await request.form()

    # ── CSRF check ────────────────────────────────────────────────
    csrf_token = (form.get(_CSRF_TOKEN_FIELD) or "").strip()
    if not _validate_csrf_token(request, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    _consume_csrf_token(request)

    username = _sanitize_username((form.get("username") or ""))
    password = (form.get("password") or "").strip()
    role = (form.get("role") or "").strip()

    if not username:
        db = SessionLocal()
        try:
            return _admin_dash_response(request, db, "Username cannot be empty")
        finally:
            db.close()

    if not validate_password_length(password):
        db = SessionLocal()
        try:
            return _admin_dash_response(
                request, db, "Password must be at least 6 characters"
            )
        finally:
            db.close()

    db = SessionLocal()
    try:
        if db.execute(users.select().where(users.c.username == username)).fetchone():
            return _admin_dash_response(
                request, db, f"Username '{username}' already exists"
            )

        now = datetime.now(timezone.utc)
        db.execute(
            users.insert().values(
                username=username,
                password=hash_password(password),
                role=role,
                created_at=now,
                updated_at=now,
                login_count=0,
                is_active=True,
            )
        )
        db.commit()
        return _admin_dash_response(
            request, db, f"User '{username}' added successfully"
        )
    except Exception as e:
        log.error("Error adding user: %s", e)
        return _admin_dash_response(request, db, "Error adding user")
    finally:
        db.close()


@app.post("/admin/update_user")
async def update_user(request: Request):
    if request.session.get("user", {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    form = await request.form()

    # ── CSRF check ────────────────────────────────────────────────
    csrf_token = (form.get(_CSRF_TOKEN_FIELD) or "").strip()
    if not _validate_csrf_token(request, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    _consume_csrf_token(request)

    user_id_raw = form.get("user_id") or ""
    try:
        user_id = int(user_id_raw)
    except (ValueError, TypeError):
        db = SessionLocal()
        try:
            return _admin_dash_response(request, db, "Invalid user ID")
        finally:
            db.close()
    new_username = _sanitize_username((form.get("username") or ""))
    new_password = (form.get("password") or "").strip()

    if new_password and not validate_password_length(new_password):
        db = SessionLocal()
        try:
            return _admin_dash_response(
                request, db, "Password must be at least 6 characters"
            )
        finally:
            db.close()

    db = SessionLocal()
    try:
        existing = db.execute(
            users.select()
            .where(users.c.username == new_username)
            .where(users.c.id != user_id)
        ).fetchone()
        if existing:
            return _admin_dash_response(
                request, db, f"Username '{new_username}' already exists"
            )

        update_values = {
            "username": new_username,
            "updated_at": datetime.now(timezone.utc),
        }
        if new_password:
            update_values["password"] = hash_password(new_password)

        db.execute(users.update().where(users.c.id == user_id).values(**update_values))
        db.commit()
        return _admin_dash_response(request, db, "User updated successfully")
    except Exception as e:
        log.error("Error updating user: %s", e)
        db.rollback()
        return _admin_dash_response(request, db, "Error updating user")
    finally:
        db.close()


@app.post("/admin/delete_user")
async def delete_user(request: Request):
    if request.session.get("user", {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    form = await request.form()

    # ── CSRF check ────────────────────────────────────────────────
    csrf_token = (form.get(_CSRF_TOKEN_FIELD) or "").strip()
    if not _validate_csrf_token(request, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    _consume_csrf_token(request)

    user_id_raw = form.get("user_id") or ""
    try:
        user_id = int(user_id_raw)
    except (ValueError, TypeError):
        db = SessionLocal()
        try:
            return _admin_dash_response(request, db, "Invalid user ID")
        finally:
            db.close()
    current_user = request.session.get("user", {}).get("username")

    db = SessionLocal()
    try:
        user_to_delete = db.execute(
            users.select().where(users.c.id == user_id)
        ).fetchone()
        if not user_to_delete:
            return _admin_dash_response(request, db, "User not found")

        if user_to_delete.username == current_user:
            return _admin_dash_response(request, db, "Cannot delete your own account")

        db.execute(users.delete().where(users.c.id == user_id))
        db.commit()
        return _admin_dash_response(
            request, db, f"User '{user_to_delete.username}' deleted"
        )
    except Exception as e:
        log.error("Error deleting user: %s", e)
        db.rollback()
        return _admin_dash_response(request, db, "Error deleting user")
    finally:
        db.close()


@app.post("/admin/ban_user")
async def ban_user(request: Request):
    if request.session.get("user", {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    form = await request.form()

    # ── CSRF check ────────────────────────────────────────────────
    csrf_token = (form.get(_CSRF_TOKEN_FIELD) or "").strip()
    if not _validate_csrf_token(request, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    _consume_csrf_token(request)

    user_id_raw = form.get("user_id") or ""
    try:
        user_id = int(user_id_raw)
    except (ValueError, TypeError):
        db = SessionLocal()
        try:
            return _admin_dash_response(request, db, "Invalid user ID")
        finally:
            db.close()
    current_user = request.session.get("user", {}).get("username")

    db = SessionLocal()
    try:
        target = db.execute(users.select().where(users.c.id == user_id)).fetchone()
        if not target:
            return _admin_dash_response(request, db, "User not found")

        if target.username == current_user:
            return _admin_dash_response(request, db, "Cannot ban yourself")

        # Toggle is_active
        currently_active = getattr(target, "is_active", True)
        new_status = not currently_active
        db.execute(
            users.update()
            .where(users.c.id == user_id)
            .values(is_active=new_status, updated_at=datetime.now(timezone.utc))
        )
        db.commit()

        # If banning, also kill all their active sessions
        if not new_status:
            db.execute(
                login_sessions.update()
                .where(login_sessions.c.username == target.username)
                .where(login_sessions.c.is_active == True)  # noqa: E712
                .values(is_active=False)
            )
            db.commit()

        action = "unbanned" if new_status else "banned"
        return _admin_dash_response(request, db, f"User '{target.username}' {action}")
    except Exception as e:
        log.error("Error banning user: %s", e)
        db.rollback()
        return _admin_dash_response(request, db, "Error updating user status")
    finally:
        db.close()


# ── User web login ────────────────────────────────────────────────────


@app.get("/user/login", response_class=HTMLResponse)
async def user_login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        "user_login.html", {"request": request, "error": error}
    )


@app.post("/user/login")
async def user_login(request: Request):
    form = await request.form()
    username = _sanitize_username((form.get("username") or ""))
    password = (form.get("password") or "").strip()

    if not username or not validate_password_length(password):
        return templates.TemplateResponse(
            "user_login.html",
            {"request": request, "error": "Invalid credentials"},
        )

    client_ip = _get_client_ip(request)
    db = SessionLocal()
    try:
        # ── rate limit ────────────────────────────────────────────
        is_locked, seconds_left = _check_rate_limit(db, f"user:{client_ip}")
        if is_locked:
            mins = (seconds_left // 60) + (1 if seconds_left % 60 else 0)
            return templates.TemplateResponse(
                "user_login.html",
                {
                    "request": request,
                    "error": f"Too many failed attempts. Try again in {mins} min.",
                },
            )

        # ── banned check ──────────────────────────────────────────
        if _is_user_banned(db, username):
            return templates.TemplateResponse(
                "user_login.html",
                {
                    "request": request,
                    "error": "Your account has been banned. Contact an administrator.",
                },
            )

        user = _find_user_by_credentials(db, username, password)
        if user:
            _record_attempt(db, f"user:{client_ip}", True)
            _update_user_login_stats(db, user.id, client_ip)

            # ── login cooldown — block rapid re-login from another device ─
            is_cooled, secs_left = _check_login_cooldown(db, username)
            if is_cooled:
                return templates.TemplateResponse(
                    "user_login.html",
                    {
                        "request": request,
                        "error": f"Account already active on another device. Try again in {secs_left}s.",
                    },
                )

            # ── deactivate old web sessions ───────────────────────────
            db.execute(
                login_sessions.update()
                .where(login_sessions.c.username == username)
                .where(login_sessions.c.is_active == True)  # noqa: E712
                .values(is_active=False)
            )
            db.commit()

            request.session["user"] = {"username": username, "role": user.role}
            return templates.TemplateResponse(
                "user_dashboard.html",
                {"request": request, "username": username},
            )
        else:
            _record_attempt(db, f"user:{client_ip}", False)
            return templates.TemplateResponse(
                "user_login.html",
                {"request": request, "error": "Invalid credentials"},
            )
    except Exception as e:
        log.error("User login error: %s", e)
        return templates.TemplateResponse(
            "user_login.html",
            {"request": request, "error": "Server error during login"},
        )
    finally:
        db.close()


@app.get("/user/dashboard", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    if request.session.get("user", {}).get("role") != "user":
        raise HTTPException(status_code=403, detail="Not authorized")
    return templates.TemplateResponse(
        "user_dashboard.html",
        {"request": request, "username": request.session["user"]["username"]},
    )


@app.get("/user/logout")
async def user_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/user/login", status_code=303)


# ══════════════════════════════════════════════════════════════════════
#  LEGACY API — /api/authenticate  (kept for backward compat)
# ══════════════════════════════════════════════════════════════════════


@app.post("/api/authenticate")
async def authenticate_user(request: Request):
    """Legacy auth endpoint — now rate-limited."""
    client_ip = _get_client_ip(request)
    try:
        form = await request.form()
        username = form.get("username")
        password = form.get("password")

        if not username or not password:
            return JSONResponse(
                content={"status": "error", "message": "Missing username or password"},
                status_code=400,
            )

        username = username.strip()
        password = password.strip()

        if not validate_password_length(password):
            return JSONResponse(
                content={"status": "error", "message": "Invalid credentials"},
                status_code=401,
            )

        db = SessionLocal()
        try:
            # ── banned check ──────────────────────────────────────
            if _is_user_banned(db, username):
                return JSONResponse(
                    content={
                        "status": "error",
                        "code": "ACCOUNT_BANNED",
                        "message": "Your account has been banned. Contact an administrator.",
                    },
                    status_code=403,
                )

            # ── rate limit ────────────────────────────────────────
            is_locked, seconds_left = _check_rate_limit(db, f"api:{client_ip}")
            if is_locked:
                mins = (seconds_left // 60) + (1 if seconds_left % 60 else 0)
                return JSONResponse(
                    content={
                        "status": "error",
                        "code": "RATE_LIMITED",
                        "message": f"Too many failed attempts. Try again in {mins} min.",
                        "retry_after_seconds": seconds_left,
                    },
                    status_code=429,
                )

            user = db.execute(
                users.select().where(users.c.username == username)
            ).fetchone()

            if user and verify_password(password, user.password):
                _record_attempt(db, f"api:{client_ip}", True)
                return JSONResponse(
                    content={
                        "status": "success",
                        "username": username,
                        "role": user.role,
                    }
                )
            else:
                _record_attempt(db, f"api:{client_ip}", False)
                return JSONResponse(
                    content={"status": "error", "message": "Invalid credentials"},
                    status_code=401,
                )
        except Exception as e:
            log.error("API auth error: %s", e)
            return JSONResponse(
                content={"status": "error", "message": "Server error"},
                status_code=500,
            )
        finally:
            db.close()
    except Exception as e:
        log.error("API parse error: %s", e)
        return JSONResponse(
            content={"status": "error", "message": "Invalid request format"},
            status_code=400,
        )


# ══════════════════════════════════════════════════════════════════════
#  /api/app/*  — DESKTOP APP ENDPOINTS
#
#  All login responses are HMAC-signed so the client can detect
#  if a MITM proxy tampered with the body.
# ══════════════════════════════════════════════════════════════════════


@app.post("/api/app/login")
async def app_login(request: Request):
    """
    Desktop app login.

    Request  JSON:  { "username": "...", "password": "...", "nonce": "<random>",
                      "device_info": {...} }
    Success  JSON:  { "status": "success", "username", "role", "session_token",
                      "nonce", "ts", "sig" }

    The client MUST verify  sig  before trusting this response.
    """
    client_ip = _get_client_ip(request)
    db = SessionLocal()
    try:
        # ── rate limit ────────────────────────────────────────────────
        is_locked, seconds_left = _check_rate_limit(db, client_ip)
        if is_locked:
            mins = (seconds_left // 60) + (1 if seconds_left % 60 else 0)
            return JSONResponse(
                content={
                    "status": "error",
                    "code": "RATE_LIMITED",
                    "message": f"Too many failed attempts. Try again in {mins} min.",
                    "retry_after_seconds": seconds_left,
                },
                status_code=429,
            )

        # ── parse body ────────────────────────────────────────────────
        try:
            body = await request.json()
        except Exception:
            form = await request.form()
            body = dict(form)

        req_username = _sanitize_username((body.get("username") or ""))
        password = (body.get("password") or "").strip()
        client_nonce = (body.get("nonce") or "").strip()
        device_raw = body.get("device_info") or {}
        if isinstance(device_raw, str):
            try:
                device_raw = _json.loads(device_raw)
            except Exception:
                device_raw = {}

        if not req_username:
            return JSONResponse(
                content={"status": "error", "message": "Username is required"},
                status_code=400,
            )
        if not password:
            return JSONResponse(
                content={"status": "error", "message": "Password is required"},
                status_code=400,
            )
        if not client_nonce:
            return JSONResponse(
                content={"status": "error", "message": "Nonce is required"},
                status_code=400,
            )
        if not validate_password_length(password):
            _record_attempt(db, client_ip, False)
            return JSONResponse(
                content={"status": "error", "message": "Invalid credentials"},
                status_code=401,
            )

        # ── banned check ──────────────────────────────────────────────
        if _is_user_banned(db, req_username):
            return JSONResponse(
                content={
                    "status": "error",
                    "code": "ACCOUNT_BANNED",
                    "message": "Your account has been banned. Contact an administrator.",
                },
                status_code=403,
            )

        # ── find user by username + password ──────────────────────────
        user = _find_user_by_credentials(db, req_username, password)

        if not user:
            _record_attempt(db, client_ip, False)
            is_locked_now, secs = _check_rate_limit(db, client_ip)
            resp = {"status": "error", "message": "Invalid credentials"}
            if is_locked_now:
                resp["code"] = "RATE_LIMITED"
                resp["retry_after_seconds"] = secs
                resp["message"] = (
                    f"Too many failed attempts. Try again in {(secs // 60) + 1} min."
                )
                return JSONResponse(content=resp, status_code=429)
            return JSONResponse(content=resp, status_code=401)

        username = user.username
        role = user.role

        # ── record success + update login stats ───────────────────────
        _record_attempt(db, client_ip, True)
        _update_user_login_stats(db, user.id, client_ip)

        # ── login cooldown — block rapid re-login from another device ─
        is_cooled, secs_left = _check_login_cooldown(db, username)
        if is_cooled:
            return JSONResponse(
                content={
                    "status": "error",
                    "code": "LOGIN_COOLDOWN",
                    "message": f"Account already active. Try again in {secs_left}s.",
                    "retry_after_seconds": secs_left,
                },
                status_code=429,
            )

        # ── deactivate old sessions (single-session) ──────────────────
        db.execute(
            login_sessions.update()
            .where(login_sessions.c.username == username)
            .where(login_sessions.c.is_active == True)  # noqa: E712
            .values(is_active=False)
        )
        db.commit()

        # ── create new session ────────────────────────────────────────
        token = secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)

        db.execute(
            login_sessions.insert().values(
                username=username,
                session_token=token,
                ip_address=client_ip,
                device_os=str(device_raw.get("device_os", ""))[:100] or None,
                device_name=str(device_raw.get("device_name", ""))[:100] or None,
                hostname=str(device_raw.get("hostname", ""))[:100] or None,
                mac_address=str(device_raw.get("mac_address", ""))[:50] or None,
                screen_resolution=str(device_raw.get("screen_resolution", ""))[:30]
                or None,
                python_version=str(device_raw.get("python_version", ""))[:30] or None,
                app_version=str(device_raw.get("app_version", ""))[:30] or None,
                logged_in_at=now,
                last_active_at=now,
                is_active=True,
            )
        )
        db.commit()

        # ── HMAC sign the response ────────────────────────────────────
        ts, sig = _sign_response(client_nonce, username, token)

        log.info("App login OK: user=%s  ip=%s", username, client_ip)
        return JSONResponse(
            content={
                "status": "success",
                "username": username,
                "role": role,
                "session_token": token,
                "nonce": client_nonce,
                "ts": ts,
                "sig": sig,
            }
        )

    except Exception as e:
        log.error("Error in /api/app/login: %s", e)
        return JSONResponse(
            content={"status": "error", "message": "Server error"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/api/app/validate_session")
async def app_validate_session(request: Request):
    """
    Validate a saved session token (for auto-login).

    Request  JSON:  { "session_token": "...", "nonce": "<random>" }
    Success  JSON:  { "status": "success", "username", "role",
                      "nonce", "ts", "sig" }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content={"status": "error", "message": "Invalid request"},
            status_code=400,
        )

    token = (body.get("session_token") or "").strip()
    client_nonce = (body.get("nonce") or "").strip()

    if not token:
        return JSONResponse(
            content={"status": "error", "message": "Missing session_token"},
            status_code=400,
        )
    if not client_nonce:
        return JSONResponse(
            content={"status": "error", "message": "Missing nonce"},
            status_code=400,
        )

    db = SessionLocal()
    try:
        row = db.execute(
            login_sessions.select()
            .where(login_sessions.c.session_token == token)
            .where(login_sessions.c.is_active == True)  # noqa: E712
        ).fetchone()

        if not row:
            # Check if the token exists but was deactivated (kicked by another login)
            kicked_row = db.execute(
                login_sessions.select()
                .where(login_sessions.c.session_token == token)
                .where(login_sessions.c.is_active == False)  # noqa: E712
            ).fetchone()

            if kicked_row:
                # Check if the user was banned after the session was created
                if _is_user_banned(db, kicked_row.username):
                    return JSONResponse(
                        content={
                            "status": "error",
                            "code": "ACCOUNT_BANNED",
                            "message": "Your account has been banned. Contact an administrator.",
                        },
                        status_code=403,
                    )

                return JSONResponse(
                    content={
                        "status": "error",
                        "code": "SESSION_KICKED",
                        "message": "Your account was logged in from another device. You have been logged out.",
                    },
                    status_code=401,
                )

            return JSONResponse(
                content={"status": "error", "message": "Session invalid or expired"},
                status_code=401,
            )

        # ── banned check on active session ────────────────────────────
        if _is_user_banned(db, row.username):
            # Deactivate the session immediately
            db.execute(
                login_sessions.update()
                .where(login_sessions.c.id == row.id)
                .values(is_active=False)
            )
            db.commit()
            return JSONResponse(
                content={
                    "status": "error",
                    "code": "ACCOUNT_BANNED",
                    "message": "Your account has been banned. Contact an administrator.",
                },
                status_code=403,
            )

        db.execute(
            login_sessions.update()
            .where(login_sessions.c.id == row.id)
            .values(last_active_at=datetime.now(timezone.utc))
        )
        db.commit()

        # ── sign ──────────────────────────────────────────────────────
        ts, sig = _sign_response(client_nonce, row.username, token)

        return JSONResponse(
            content={
                "status": "success",
                "username": row.username,
                "role": "user",
                "nonce": client_nonce,
                "ts": ts,
                "sig": sig,
            }
        )
    except Exception as e:
        log.error("Error in validate_session: %s", e)
        return JSONResponse(
            content={"status": "error", "message": "Server error"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/api/app/logout")
async def app_logout(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content={"status": "error", "message": "Invalid request"},
            status_code=400,
        )

    token = (body.get("session_token") or "").strip()
    if not token:
        return JSONResponse(
            content={"status": "error", "message": "Missing session_token"},
            status_code=400,
        )

    db = SessionLocal()
    try:
        db.execute(
            login_sessions.update()
            .where(login_sessions.c.session_token == token)
            .values(is_active=False)
        )
        db.commit()
        return JSONResponse(content={"status": "success", "message": "Logged out"})
    except Exception as e:
        log.error("Error in logout: %s", e)
        return JSONResponse(
            content={"status": "error", "message": "Server error"},
            status_code=500,
        )
    finally:
        db.close()


@app.get("/api/app/ping")
async def app_ping():
    return JSONResponse(content={"status": "ok"})


# ══════════════════════════════════════════════════════════════════════
#  SMART POLLING — lightweight version check to avoid full data fetches
# ══════════════════════════════════════════════════════════════════════


def _compute_table_hash(db, table, username: str) -> str:
    """
    Compute a lightweight hash of a user's rows in a table.
    Uses COUNT + MAX(id) which is extremely fast (index-only scan)
    and changes whenever a row is added or deleted.
    """
    try:
        row = db.execute(
            text(
                f"SELECT COUNT(*) AS cnt, COALESCE(MAX(id), 0) AS max_id "
                f"FROM {table} WHERE username = :u"
            ),
            {"u": username},
        ).fetchone()
        return f"{row.cnt}:{row.max_id}"
    except Exception:
        return "0:0"


@app.get("/api/poll/{username}")
async def smart_poll(username: str, request: Request):
    """
    Lightweight polling endpoint.  Returns tiny version hashes for each
    data category.  The client compares these against its cached versions
    and only fetches full data (from the normal endpoints) when a hash
    has changed.

    Response ~120 bytes vs ~2-10 KB for full history endpoints.
    """
    if not _authorize_clipboard_access(request, username):
        raise HTTPException(status_code=403, detail="Not authorized")

    db = SessionLocal()
    try:
        copied_v = _compute_table_hash(db, "copied_text_history", username)
        submitted_v = _compute_table_hash(db, "submitted_text_history", username)
        clipboard_v = _compute_table_hash(db, "clipboard_updates", username)

        return JSONResponse(
            content={
                "status": "ok",
                "v": {
                    "copied": copied_v,
                    "submitted": submitted_v,
                    "clipboard": clipboard_v,
                },
            },
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            },
        )
    except Exception as e:
        log.error("Poll error for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Poll failed"},
            status_code=500,
        )
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════
#  CLIPBOARD API
# ══════════════════════════════════════════════════════════════════════


def _authorize_clipboard_access(request: Request, username: str) -> bool:
    """
    Check if the request is authorized to access a user's clipboard data.
    Allows: the user themselves (web session), or an admin, or a valid
    app session token passed as a Bearer header.
    Blocked/banned users are always denied.
    """
    # 0. Banned users are always denied
    db_check = SessionLocal()
    try:
        if _is_user_banned(db_check, username):
            return False
    finally:
        db_check.close()

    # 1. Web session — user accessing own data
    session_user = request.session.get("user", {})
    if session_user.get("username") == username:
        return True
    # 2. Admin web session
    if session_user.get("role") == "admin":
        return True
    # 3. App session token via Authorization header (desktop client)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            db = SessionLocal()
            try:
                row = db.execute(
                    login_sessions.select()
                    .where(login_sessions.c.session_token == token)
                    .where(login_sessions.c.username == username)
                    .where(login_sessions.c.is_active == True)  # noqa: E712
                ).fetchone()
                if row:
                    return True
            except Exception:
                pass
            finally:
                db.close()
    return False


@app.get("/api/copied_text_history/{username}")
async def get_copied_text_history(username: str, request: Request):
    if not _authorize_clipboard_access(request, username):
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        items = db.execute(
            copied_text_history.select()
            .where(copied_text_history.c.username == username)
            .order_by(copied_text_history.c.id.desc())
        ).fetchall()
        return JSONResponse(
            content={
                "status": "success",
                "copied_text_history": [i.text for i in items],
            }
        )
    except Exception as e:
        log.error("Error fetching copied text for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error fetching copied text"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/api/submit_to_clipboard/{username}")
async def submit_to_clipboard(username: str, item: HistoryItem, request: Request):
    if not _authorize_clipboard_access(request, username):
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(clipboard_updates.insert().values(username=username, text=item.text))
        db.commit()

        items = db.execute(
            clipboard_updates.select()
            .where(clipboard_updates.c.username == username)
            .order_by(clipboard_updates.c.id)
        ).fetchall()
        if len(items) > 1:
            to_del = len(items) - 1
            db.execute(
                clipboard_updates.delete()
                .where(clipboard_updates.c.username == username)
                .where(clipboard_updates.c.id.in_([i.id for i in items[:to_del]]))
            )
            db.commit()
        return JSONResponse(
            content={"status": "success", "message": "Text sent to clipboard"}
        )
    except Exception as e:
        log.error("Error submitting clipboard for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error submitting clipboard"},
            status_code=500,
        )
    finally:
        db.close()


@app.get("/api/get_latest_clipboard/{username}")
async def get_latest_clipboard(username: str, request: Request):
    if not _authorize_clipboard_access(request, username):
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        latest = db.execute(
            clipboard_updates.select()
            .where(clipboard_updates.c.username == username)
            .order_by(clipboard_updates.c.id.desc())
        ).first()
        if latest:
            return JSONResponse(content={"status": "success", "text": latest.text})
        return JSONResponse(content={"status": "success", "text": ""})
    except Exception as e:
        log.error("Error fetching clipboard for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error fetching clipboard"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/api/submit_copied_text/{username}")
async def submit_copied_text(username: str, item: HistoryItem, request: Request):
    if not _authorize_clipboard_access(request, username):
        raise HTTPException(status_code=403, detail="Not authorized")

    # ── Input size limit (prevent DoS via huge payloads) ──────────
    if len(item.text) > 10_000:
        return JSONResponse(
            content={"status": "error", "message": "Text too large (max 10KB)"},
            status_code=413,
        )

    db = SessionLocal()
    try:
        db.execute(
            copied_text_history.insert().values(username=username, text=item.text)
        )
        db.commit()

        items = db.execute(
            copied_text_history.select()
            .where(copied_text_history.c.username == username)
            .order_by(copied_text_history.c.id)
        ).fetchall()
        if len(items) > 10:
            to_del = len(items) - 10
            db.execute(
                copied_text_history.delete()
                .where(copied_text_history.c.username == username)
                .where(copied_text_history.c.id.in_([i.id for i in items[:to_del]]))
            )
            db.commit()
        return JSONResponse(
            content={"status": "success", "message": "Copied text submitted"}
        )
    except Exception as e:
        log.error("Error submitting copied text for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error submitting data"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/api/delete_copied_text/{username}")
async def delete_copied_text(username: str, item: HistoryItem, request: Request):
    if not _authorize_clipboard_access(request, username):
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            copied_text_history.delete()
            .where(copied_text_history.c.username == username)
            .where(copied_text_history.c.text == item.text)
        )
        db.commit()
        return JSONResponse(
            content={"status": "success", "message": "Copied text deleted"}
        )
    except Exception as e:
        log.error("Error deleting copied text for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error deleting copied text"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/api/clear_copied_text/{username}")
async def clear_copied_text(username: str, request: Request):
    if not _authorize_clipboard_access(request, username):
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            copied_text_history.delete().where(
                copied_text_history.c.username == username
            )
        )
        db.commit()
        return JSONResponse(
            content={"status": "success", "message": "Copied text cleared"}
        )
    except Exception as e:
        log.error("Error clearing copied text for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error clearing copied text"},
            status_code=500,
        )
    finally:
        db.close()


@app.get("/api/submitted_text_history/{username}")
async def get_submitted_text_history(username: str, request: Request):
    if not _authorize_clipboard_access(request, username):
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        items = db.execute(
            submitted_text_history.select()
            .where(submitted_text_history.c.username == username)
            .order_by(submitted_text_history.c.id.desc())
        ).fetchall()
        return JSONResponse(
            content={
                "status": "success",
                "submitted_text_history": [i.text for i in items],
            }
        )
    except Exception as e:
        log.error("Error fetching submitted text for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error fetching submitted text"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/api/submit_submitted_text/{username}")
async def submit_submitted_text(username: str, item: HistoryItem, request: Request):
    if not _authorize_clipboard_access(request, username):
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            submitted_text_history.insert().values(username=username, text=item.text)
        )
        db.commit()

        items = db.execute(
            submitted_text_history.select()
            .where(submitted_text_history.c.username == username)
            .order_by(submitted_text_history.c.id)
        ).fetchall()
        if len(items) > 10:
            to_del = len(items) - 10
            db.execute(
                submitted_text_history.delete()
                .where(submitted_text_history.c.username == username)
                .where(submitted_text_history.c.id.in_([i.id for i in items[:to_del]]))
            )
            db.commit()
        return JSONResponse(
            content={"status": "success", "message": "Submitted text saved"}
        )
    except Exception as e:
        log.error("Error submitting text for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error submitting text"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/api/delete_submitted_text/{username}")
async def delete_submitted_text(username: str, item: HistoryItem, request: Request):
    if not _authorize_clipboard_access(request, username):
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            submitted_text_history.delete()
            .where(submitted_text_history.c.username == username)
            .where(submitted_text_history.c.text == item.text)
        )
        db.commit()
        return JSONResponse(
            content={"status": "success", "message": "Submitted text deleted"}
        )
    except Exception as e:
        log.error("Error deleting submitted text for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error deleting submitted text"},
            status_code=500,
        )
    finally:
        db.close()


@app.post("/api/clear_submitted_text/{username}")
async def clear_submitted_text(username: str, request: Request):
    if not _authorize_clipboard_access(request, username):
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            submitted_text_history.delete().where(
                submitted_text_history.c.username == username
            )
        )
        db.commit()
        return JSONResponse(
            content={"status": "success", "message": "Submitted text cleared"}
        )
    except Exception as e:
        log.error("Error clearing submitted text for %s: %s", username, e)
        return JSONResponse(
            content={"status": "error", "message": "Error clearing submitted text"},
            status_code=500,
        )
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════


def _get_all_users(db):
    return db.execute(users.select().order_by(users.c.id)).fetchall()


def _is_sqlite() -> bool:
    """Check if the current database engine is SQLite."""
    return "sqlite" in str(engine.url)


def _get_session_stats(db):
    """Get per-user active session counts and recent session info."""
    if _is_sqlite():
        # SQLite does not support FILTER — use CASE/SUM instead
        rows = db.execute(
            text(
                """
                SELECT username,
                       SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active_sessions,
                       MAX(logged_in_at) AS last_session_at,
                       MAX(CASE WHEN is_active = 1 THEN ip_address ELSE NULL END) AS active_ip
                FROM login_sessions
                GROUP BY username
                """
            )
        ).fetchall()
    else:
        rows = db.execute(
            text(
                """
                SELECT username,
                       COUNT(*) FILTER (WHERE is_active = TRUE)  AS active_sessions,
                       MAX(logged_in_at)                          AS last_session_at,
                       MAX(ip_address) FILTER (WHERE is_active = TRUE) AS active_ip
                FROM login_sessions
                GROUP BY username
                """
            )
        ).fetchall()
    return {r.username: r for r in rows}


def _get_text_counts(db):
    """Get per-user counts of copied text and submitted text history."""
    result = {}
    try:
        copied_rows = db.execute(
            text(
                "SELECT username, COUNT(*) AS cnt FROM copied_text_history GROUP BY username"
            )
        ).fetchall()
        for r in copied_rows:
            result.setdefault(r.username, {"copied": 0, "submitted": 0})
            result[r.username]["copied"] = r.cnt
    except Exception:
        pass
    try:
        submitted_rows = db.execute(
            text(
                "SELECT username, COUNT(*) AS cnt FROM submitted_text_history GROUP BY username"
            )
        ).fetchall()
        for r in submitted_rows:
            result.setdefault(r.username, {"copied": 0, "submitted": 0})
            result[r.username]["submitted"] = r.cnt
    except Exception:
        pass
    return result


def _get_total_stats(db):
    """Dashboard-level aggregate stats."""
    total_users = db.execute(text("SELECT COUNT(*) FROM users")).scalar() or 0
    active_users = (
        db.execute(text("SELECT COUNT(*) FROM users WHERE is_active = TRUE")).scalar()
        or 0
    )
    banned_users = (
        db.execute(text("SELECT COUNT(*) FROM users WHERE is_active = FALSE")).scalar()
        or 0
    )
    total_sessions = (
        db.execute(
            text("SELECT COUNT(*) FROM login_sessions WHERE is_active = TRUE")
        ).scalar()
        or 0
    )
    # Use compatible date arithmetic for both PostgreSQL and SQLite
    if _is_sqlite():
        recent_logins = (
            db.execute(
                text(
                    "SELECT COUNT(*) FROM login_sessions "
                    "WHERE logged_in_at > datetime('now', '-24 hours')"
                )
            ).scalar()
            or 0
        )
    else:
        recent_logins = (
            db.execute(
                text(
                    "SELECT COUNT(*) FROM login_sessions "
                    "WHERE logged_in_at > NOW() - INTERVAL '24 hours'"
                )
            ).scalar()
            or 0
        )
    return {
        "total_users": total_users,
        "active_users": active_users,
        "banned_users": banned_users,
        "active_sessions": total_sessions,
        "logins_24h": recent_logins,
    }


def _admin_dash_response(request, db, message=None):
    """Build the admin dashboard template response with all data."""
    all_users = _get_all_users(db)
    try:
        session_stats = _get_session_stats(db)
    except Exception:
        session_stats = {}
    try:
        stats = _get_total_stats(db)
    except Exception:
        stats = {
            "total_users": len(all_users),
            "active_users": 0,
            "banned_users": 0,
            "active_sessions": 0,
            "logins_24h": 0,
        }
    try:
        text_counts = _get_text_counts(db)
    except Exception:
        text_counts = {}

    # Generate CSRF token for all forms
    csrf_token = _generate_csrf_token(request)

    ctx = {
        "request": request,
        "users": all_users,
        "session_stats": session_stats,
        "stats": stats,
        "text_counts": text_counts,
        "csrf_token": csrf_token,
    }
    if message:
        ctx["message"] = message
    return templates.TemplateResponse("admin_dashboard.html", ctx)
