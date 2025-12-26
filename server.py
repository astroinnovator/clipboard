import os
import time
from datetime import datetime, timedelta
from typing import Dict, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator
from sqlalchemy import (
    Column,
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

# Load environment variables
load_dotenv()

# Initialize FastAPI app
app = FastAPI()

# Add CORS middleware
# Get CORS origins from environment - REQUIRED
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
# Get session secret key from environment - REQUIRED
SESSION_SECRET = os.getenv("SESSION_SECRET_KEY")
if not SESSION_SECRET:
    raise ValueError("SESSION_SECRET_KEY environment variable must be set")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)


# Get default admin credentials from environment - REQUIRED
DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME")
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD")

if not DEFAULT_ADMIN_USERNAME or not DEFAULT_ADMIN_PASSWORD:
    raise ValueError(
        "DEFAULT_ADMIN_USERNAME and DEFAULT_ADMIN_PASSWORD environment variables must be set"
    )

# Get default user credentials from environment - REQUIRED
DEFAULT_USER_USERNAME = os.getenv("DEFAULT_USER_USERNAME")
DEFAULT_USER_PASSWORD = os.getenv("DEFAULT_USER_PASSWORD")

if not DEFAULT_USER_USERNAME or not DEFAULT_USER_PASSWORD:
    raise ValueError(
        "DEFAULT_USER_USERNAME and DEFAULT_USER_PASSWORD environment variables must be set"
    )

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Set up templates
templates = Jinja2Templates(directory="templates")

# In-memory cache for rate limiting and login attempts
# Structure: {username: {"attempts": count, "blocked_until": timestamp}}
login_attempts_cache: Dict[str, Dict] = {}

# In-memory cache for user data to speed up data retrieval
# Structure: {username: {"profile": user_data, "copied_text": [], "submitted_text": [], "clipboard": [], "last_updated": timestamp}}
user_data_cache: Dict[str, Dict] = {}
CACHE_TTL = 300  # Cache time-to-live in seconds (5 minutes)


def check_rate_limit(username: str) -> Tuple[bool, str]:
    """Check if user is rate limited. Returns (is_blocked, message)"""
    if username in login_attempts_cache:
        user_data = login_attempts_cache[username]
        blocked_until = user_data.get("blocked_until")

        if blocked_until and datetime.now() < blocked_until:
            remaining_time = (blocked_until - datetime.now()).total_seconds() / 60
            return (
                True,
                f"Too many failed attempts. Try again in {int(remaining_time)} minutes.",
            )
        elif blocked_until and datetime.now() >= blocked_until:
            # Reset after block period expires
            login_attempts_cache[username] = {"attempts": 0, "blocked_until": None}

    return False, ""


def record_failed_attempt(username: str):
    """Record a failed login attempt and apply rate limiting if needed"""
    if username not in login_attempts_cache:
        login_attempts_cache[username] = {"attempts": 0, "blocked_until": None}

    login_attempts_cache[username]["attempts"] += 1

    if login_attempts_cache[username]["attempts"] >= 10:
        # Block for 30 minutes
        login_attempts_cache[username]["blocked_until"] = datetime.now() + timedelta(
            minutes=30
        )
        print(
            f"User {username} has been blocked for 30 minutes due to too many failed attempts"
        )


def reset_attempts(username: str):
    """Reset failed login attempts on successful login"""
    if username in login_attempts_cache:
        login_attempts_cache[username]["attempts"] = 0
        login_attempts_cache[username]["blocked_until"] = None


def validate_password_length(password: str) -> bool:
    """Validate that password is at least 6 characters"""
    return len(password) >= 6


def get_cached_user_data(username: str, data_type: str):
    """Get cached user data if available and not expired"""
    if username in user_data_cache:
        cache_entry = user_data_cache[username]
        last_updated = cache_entry.get("last_updated", 0)

        # Check if cache is still valid (not expired)
        if time.time() - last_updated < CACHE_TTL:
            return cache_entry.get(data_type)

    return None


def set_cached_user_data(username: str, data_type: str, data):
    """Cache user data with timestamp"""
    if username not in user_data_cache:
        user_data_cache[username] = {"last_updated": time.time()}

    user_data_cache[username][data_type] = data
    user_data_cache[username]["last_updated"] = time.time()


def invalidate_user_cache(username: str, data_type: str = None):
    """Invalidate specific cache or all cache for a user"""
    if username in user_data_cache:
        if data_type:
            # Remove specific data type from cache
            if data_type in user_data_cache[username]:
                del user_data_cache[username][data_type]
        else:
            # Remove entire user cache
            del user_data_cache[username]


# Database connection (Neon)
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL environment variable is not set. Please set it to your Neon database connection string."
    )

# Convert postgresql:// to postgresql+psycopg:// for SQLAlchemy compatibility
database_url = DATABASE_URL
if database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

print(f"Connecting to database with URL: {database_url}")

try:
    engine = create_engine(database_url)
    print("Database connection successful")
except Exception as e:
    print(f"Failed to connect to database: {e}")
    raise

metadata = MetaData()

# Define tables
users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(50), unique=True, nullable=False),
    Column("password", String(50), nullable=False),
    Column("role", String(10), nullable=False),
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

# Create tables and handle migrations
try:
    # First, check if we need to migrate existing tables
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()

    if "users" in existing_tables:
        # Check if secret_key column exists and remove it
        columns = inspector.get_columns("users")
        column_names = [col["name"] for col in columns]

        if "secret_key" in column_names:
            print("Removing secret_key column from users table...")
            with engine.connect() as conn:
                conn.execute(
                    text("ALTER TABLE users DROP COLUMN IF EXISTS secret_key;")
                )
                conn.commit()
            print("secret_key column removed successfully")

    # Create all tables (this will create new ones and skip existing ones)
    metadata.create_all(engine)
    print("Tables created/updated successfully")
except Exception as e:
    print(f"Error creating/updating tables: {e}")
    raise

# Set up database session
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Create default admin and user on startup
@app.on_event("startup")
async def startup_event():
    db = SessionLocal()
    try:
        print("Starting database initialization")

        # Validate default passwords meet minimum length requirement
        if not validate_password_length(DEFAULT_ADMIN_PASSWORD):
            print(f"ERROR: DEFAULT_ADMIN_PASSWORD must be at least 6 characters long!")
            raise ValueError(
                "DEFAULT_ADMIN_PASSWORD does not meet minimum length requirement"
            )

        if not validate_password_length(DEFAULT_USER_PASSWORD):
            print(f"ERROR: DEFAULT_USER_PASSWORD must be at least 6 characters long!")
            raise ValueError(
                "DEFAULT_USER_PASSWORD does not meet minimum length requirement"
            )

        # Default admin - WARNING: Change credentials in .env for production!
        if not db.execute(
            users.select().where(users.c.username == DEFAULT_ADMIN_USERNAME)
        ).fetchone():
            db.execute(
                users.insert().values(
                    username=DEFAULT_ADMIN_USERNAME,
                    password=DEFAULT_ADMIN_PASSWORD,
                    role="admin",
                )
            )
            print(
                f"Default admin created: {DEFAULT_ADMIN_USERNAME} (password length: {len(DEFAULT_ADMIN_PASSWORD)} chars)"
            )
            print("WARNING: Change default admin password in .env file for production!")
        else:
            print(f"Admin user '{DEFAULT_ADMIN_USERNAME}' already exists")

        # Default user - WARNING: Change credentials in .env for production!
        if not db.execute(
            users.select().where(users.c.username == DEFAULT_USER_USERNAME)
        ).fetchone():
            db.execute(
                users.insert().values(
                    username=DEFAULT_USER_USERNAME,
                    password=DEFAULT_USER_PASSWORD,
                    role="user",
                )
            )
            print(
                f"Default user created: {DEFAULT_USER_USERNAME} (password length: {len(DEFAULT_USER_PASSWORD)} chars)"
            )
            print("WARNING: Change default user password in .env file for production!")
        else:
            print(f"User '{DEFAULT_USER_USERNAME}' already exists")

        db.commit()

        # Log all users to verify (without showing passwords in production)
        all_users = db.execute(users.select()).fetchall()
        print("Users in database on startup:")
        for user in all_users:
            print(f"ID: {user.id}, Username: {user.username}, Role: {user.role}")
    except Exception as e:
        print(f"Error during startup: {e}")
        raise
    finally:
        db.close()


# Pydantic model for history items
class HistoryItem(BaseModel):
    text: str


# Routes
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/admin", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str | None = None):
    print("Serving admin login page")
    return templates.TemplateResponse(
        "admin_login.html", {"request": request, "error": error}
    )


@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()

    print(f"Admin login attempt - Username: {username}")

    # Check rate limiting
    is_blocked, block_message = check_rate_limit(username)
    if is_blocked:
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": block_message},
        )

    # Validate password length
    if not validate_password_length(password):
        record_failed_attempt(username)
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Invalid credentials"},
        )

    db = SessionLocal()
    try:
        user = db.execute(users.select().where(users.c.username == username)).fetchone()
        if user:
            print(f"User found - Username: {user.username}, Role: {user.role}")

            # Check password and role (no secret key required)
            password_valid = user.password == password and user.role == "admin"

            if password_valid:
                print("Login successful, setting session")
                reset_attempts(username)  # Reset failed attempts on success
                request.session["user"] = {"username": username, "role": "admin"}
                return templates.TemplateResponse(
                    "admin_dashboard.html",
                    {"request": request, "users": get_all_users(db)},
                )
            else:
                print("Login failed: Invalid username or password")
                record_failed_attempt(username)
                return templates.TemplateResponse(
                    "admin_login.html",
                    {"request": request, "error": "Invalid credentials"},
                )
        else:
            print(f"Login failed: User '{username}' not found in database")
            record_failed_attempt(username)
            return templates.TemplateResponse(
                "admin_login.html",
                {"request": request, "error": "Invalid credentials"},
            )
    except Exception as e:
        print(f"Error during admin login: {e}")
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Server error during login"},
        )
    finally:
        db.close()


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if request.session.get("user", {}).get("role") != "admin":
        print("Admin dashboard access denied: Not authorized")
        raise HTTPException(status_code=403, detail="Not authorized")

    db = SessionLocal()
    try:
        print("Serving admin dashboard")
        return templates.TemplateResponse(
            "admin_dashboard.html", {"request": request, "users": get_all_users(db)}
        )
    finally:
        db.close()


@app.get("/admin/logout")
async def admin_logout(request: Request):
    """Admin logout - clear session and redirect to admin login"""
    request.session.clear()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/add_user")
async def add_user(request: Request):
    if request.session.get("user", {}).get("role") != "admin":
        print("Add user access denied: Not authorized")
        raise HTTPException(status_code=403, detail="Not authorized")

    form = await request.form()
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()
    role = (form.get("role") or "").strip()

    print(f"Adding new user - Username: {username}, Role: {role}")

    # Validate password length
    if not validate_password_length(password):
        return templates.TemplateResponse(
            "admin_dashboard.html",
            {
                "request": request,
                "users": get_all_users(SessionLocal()),
                "message": "Password must be at least 6 characters",
            },
        )

    db = SessionLocal()
    try:
        # Check if username already exists
        if db.execute(users.select().where(users.c.username == username)).fetchone():
            print(f"Add user failed: Username '{username}' already exists")
            return templates.TemplateResponse(
                "admin_dashboard.html",
                {
                    "request": request,
                    "users": get_all_users(db),
                    "message": f"Username '{username}' already exists",
                },
            )

        # Insert the new user
        db.execute(
            users.insert().values(username=username, password=password, role=role)
        )
        db.commit()
        print("User added successfully")
        return templates.TemplateResponse(
            "admin_dashboard.html",
            {
                "request": request,
                "users": get_all_users(db),
                "message": f"User '{username}' added successfully",
            },
        )
    except Exception as e:
        print(f"Error adding user: {e}")
        return templates.TemplateResponse(
            "admin_dashboard.html",
            {
                "request": request,
                "users": get_all_users(db),
                "message": "Error adding user",
            },
        )
    finally:
        db.close()


@app.post("/admin/update_user")
async def update_user(request: Request):
    if request.session.get("user", {}).get("role") != "admin":
        print("Update user access denied: Not authorized")
        raise HTTPException(status_code=403, detail="Not authorized")

    form = await request.form()
    user_id = form.get("user_id") or ""
    new_username = (form.get("username") or "").strip()
    new_password = (form.get("password") or "").strip()

    print(f"Updating user - ID: {user_id}, New Username: {new_username}")

    # Validate password length if password is being updated
    if new_password and not validate_password_length(new_password):
        return templates.TemplateResponse(
            "admin_dashboard.html",
            {
                "request": request,
                "users": get_all_users(SessionLocal()),
                "message": "Password must be at least 6 characters",
            },
        )

    db = SessionLocal()
    try:
        # Check if the new username is already taken by another user
        existing_user = db.execute(
            users.select()
            .where(users.c.username == new_username)
            .where(users.c.id != user_id)
        ).fetchone()
        if existing_user:
            print(f"Update user failed: Username '{new_username}' already exists")
            return templates.TemplateResponse(
                "admin_dashboard.html",
                {
                    "request": request,
                    "users": get_all_users(db),
                    "message": f"Username '{new_username}' already exists",
                },
            )

        # Update the user
        update_values = {"username": new_username}
        if new_password:  # Only update password if a new one is provided
            update_values["password"] = new_password
        db.execute(users.update().where(users.c.id == user_id).values(**update_values))
        db.commit()
        print("User updated successfully")
        return templates.TemplateResponse(
            "admin_dashboard.html",
            {
                "request": request,
                "users": get_all_users(db),
                "message": "User updated successfully",
            },
        )
    except Exception as e:
        print(f"Error updating user: {e}")
        return templates.TemplateResponse(
            "admin_dashboard.html",
            {
                "request": request,
                "users": get_all_users(db),
                "message": "Error updating user",
            },
        )
    finally:
        db.close()


@app.post("/admin/delete_user")
async def delete_user(request: Request):
    if request.session.get("user", {}).get("role") != "admin":
        print("Delete user access denied: Not authorized")
        raise HTTPException(status_code=403, detail="Not authorized")

    form = await request.form()
    user_id = form.get("user_id") or ""
    current_user = request.session.get("user", {}).get("username")

    print(f"Deleting user - ID: {user_id}")

    db = SessionLocal()
    try:
        # Get the user to be deleted
        user_to_delete = db.execute(
            users.select().where(users.c.id == user_id)
        ).fetchone()
        if not user_to_delete:
            print(f"Delete user failed: User ID '{user_id}' not found")
            return templates.TemplateResponse(
                "admin_dashboard.html",
                {
                    "request": request,
                    "users": get_all_users(db),
                    "message": "User not found",
                },
            )

        # Prevent the current admin from deleting themselves
        if user_to_delete.username == current_user:
            print(
                f"Delete user failed: Cannot delete the current admin '{current_user}'"
            )
            return templates.TemplateResponse(
                "admin_dashboard.html",
                {
                    "request": request,
                    "users": get_all_users(db),
                    "message": "Cannot delete your own account",
                },
            )

        # Delete the user
        db.execute(users.delete().where(users.c.id == user_id))
        db.commit()
        print(f"User '{user_to_delete.username}' deleted successfully")
        return templates.TemplateResponse(
            "admin_dashboard.html",
            {
                "request": request,
                "users": get_all_users(db),
                "message": f"User '{user_to_delete.username}' deleted successfully",
            },
        )
    except Exception as e:
        print(f"Error deleting user: {e}")
        return templates.TemplateResponse(
            "admin_dashboard.html",
            {
                "request": request,
                "users": get_all_users(db),
                "message": "Error deleting user",
            },
        )
    finally:
        db.close()


@app.get("/user/login", response_class=HTMLResponse)
async def user_login_page(request: Request, error: str | None = None):
    print("Serving user login page")
    return templates.TemplateResponse(
        "user_login.html", {"request": request, "error": error}
    )


@app.post("/user/login")
async def user_login(request: Request):
    form = await request.form()
    password = (form.get("password") or "").strip()

    print(f"User login attempt with password")

    # Validate password length first (but show generic error)
    if not validate_password_length(password):
        return templates.TemplateResponse(
            "user_login.html",
            {"request": request, "error": "Invalid credentials"},
        )

    db = SessionLocal()
    try:
        # Find user by password match (user role only)
        user = db.execute(
            users.select()
            .where(users.c.password == password)
            .where(users.c.role == "user")
        ).fetchone()

        if user:
            username = user.username
            print(f"User found - Username: {username}, Role: {user.role}")

            # Check rate limiting for this username
            is_blocked, block_message = check_rate_limit(username)
            if is_blocked:
                return templates.TemplateResponse(
                    "user_login.html",
                    {"request": request, "error": block_message},
                )

            print("Login successful, setting session")
            reset_attempts(username)  # Reset failed attempts on success
            request.session["user"] = {"username": username, "role": "user"}
            return templates.TemplateResponse(
                "user_dashboard.html", {"request": request, "username": username}
            )
        else:
            print("Login failed: Invalid password")
            # Record failed attempt with generic identifier for password-only login
            record_failed_attempt(f"password_attempt_{hash(password) % 10000}")
            return templates.TemplateResponse(
                "user_login.html",
                {"request": request, "error": "Invalid credentials"},
            )
    except Exception as e:
        print(f"Error during user login: {e}")
        return templates.TemplateResponse(
            "user_login.html",
            {"request": request, "error": "Server error during login"},
        )
    finally:
        db.close()


@app.get("/user/dashboard", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    if request.session.get("user", {}).get("role") != "user":
        print("User dashboard access denied: Not authorized")
        raise HTTPException(status_code=403, detail="Not authorized")
    print("Serving user dashboard")
    return templates.TemplateResponse(
        "user_dashboard.html",
        {"request": request, "username": request.session["user"]["username"]},
    )


@app.get("/user/logout")
async def user_logout(request: Request):
    """User logout - clear session and redirect to user login"""
    request.session.clear()
    return RedirectResponse(url="/user/login", status_code=303)


# API endpoint to authenticate users (for the desktop app)
@app.post("/api/authenticate")
async def authenticate_user(request: Request):
    try:
        form = await request.form()
        print(f"Received form data: {dict(form)}")
        username = form.get("username")
        password = form.get("password")

        if not username or not password:
            print("Missing username or password in form data")
            return JSONResponse(
                content={
                    "status": "error",
                    "message": "Missing username or password",
                },
                status_code=400,
            )

        username = username.strip()
        password = password.strip()

        print(f"API authenticate attempt - Username: {username}")

        # Check rate limiting
        is_blocked, block_message = check_rate_limit(username)
        if is_blocked:
            return JSONResponse(
                content={
                    "status": "error",
                    "message": block_message,
                },
                status_code=429,
            )

        # Validate password length
        if not validate_password_length(password):
            record_failed_attempt(username)
            return JSONResponse(
                content={
                    "status": "error",
                    "message": "Invalid credentials",
                },
                status_code=401,
            )

        db = SessionLocal()
        try:
            user = db.execute(
                users.select().where(users.c.username == username)
            ).fetchone()

            if user and user.password == password:
                print(f"API authentication successful for user: {username}")
                reset_attempts(username)  # Reset failed attempts on success
                return JSONResponse(
                    content={
                        "status": "success",
                        "username": username,
                        "role": user.role,
                    }
                )
            else:
                print(f"API authentication failed for user: {username}")
                record_failed_attempt(username)
                return JSONResponse(
                    content={
                        "status": "error",
                        "message": "Invalid credentials",
                    },
                    status_code=401,
                )
        except Exception as e:
            print(f"Error during API authentication: {e}")
            return JSONResponse(
                content={"status": "error", "message": "Server error"}, status_code=500
            )
        finally:
            db.close()
    except Exception as e:
        print(f"Error parsing form data: {e}")
        return JSONResponse(
            content={"status": "error", "message": "Invalid request format"},
            status_code=400,
        )


# API endpoint to fetch copied text history for a user (Text Viewer)
@app.get("/api/copied_text_history/{username}")
async def get_copied_text_history(username: str, request: Request = None):
    # Try to get from cache first
    cached_data = get_cached_user_data(username, "copied_text_history")
    if cached_data is not None:
        print(f"Returning cached copied text history for {username}")
        return JSONResponse(
            content={
                "status": "success",
                "copied_text_history": cached_data,
            }
        )

    db = SessionLocal()
    try:
        copied_text_items = db.execute(
            copied_text_history.select()
            .where(copied_text_history.c.username == username)
            .order_by(copied_text_history.c.id.desc())
        ).fetchall()

        # Cache the result
        history_list = [item.text for item in copied_text_items]
        set_cached_user_data(username, "copied_text_history", history_list)

        return JSONResponse(
            content={
                "status": "success",
                "copied_text_history": history_list,
            }
        )
    except Exception as e:
        print(f"Error fetching copied text history for {username}: {e}")
        return JSONResponse(
            content={
                "status": "error",
                "message": "Error fetching copied text history",
            },
            status_code=500,
        )
    finally:
        db.close()


# API endpoint to submit text to clipboard (from Clipboard Manager)
@app.post("/api/submit_to_clipboard/{username}")
async def submit_to_clipboard(username: str, item: HistoryItem, request: Request):
    if "user" not in request.session or request.session["user"]["username"] != username:
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        # Store the text in clipboard_updates table
        db.execute(clipboard_updates.insert().values(username=username, text=item.text))
        db.commit()

        # Invalidate clipboard cache
        invalidate_user_cache(username, "clipboard_latest")

        # Enforce only the latest text (delete older entries)
        items = db.execute(
            clipboard_updates.select()
            .where(clipboard_updates.c.username == username)
            .order_by(clipboard_updates.c.id)
        ).fetchall()
        if len(items) > 1:
            items_to_delete = len(items) - 1
            db.execute(
                clipboard_updates.delete()
                .where(clipboard_updates.c.username == username)
                .where(
                    clipboard_updates.c.id.in_(
                        [item.id for item in items[:items_to_delete]]
                    )
                )
            )
            db.commit()
        return JSONResponse(
            content={"status": "success", "message": "Text sent to clipboard"}
        )
    except Exception as e:
        print(f"Error submitting text to clipboard for {username}: {e}")
        return JSONResponse(
            content={
                "status": "error",
                "message": "Error submitting text to clipboard",
            },
            status_code=500,
        )
    finally:
        db.close()


# API endpoint to get the latest clipboard text (for polling)
@app.get("/api/get_latest_clipboard/{username}")
async def get_latest_clipboard(username: str):
    # Try to get from cache first
    cached_data = get_cached_user_data(username, "clipboard_latest")
    if cached_data is not None:
        return JSONResponse(content={"status": "success", "text": cached_data})

    db = SessionLocal()
    try:
        latest_item = db.execute(
            clipboard_updates.select()
            .where(clipboard_updates.c.username == username)
            .order_by(clipboard_updates.c.id.desc())
        ).first()

        text = latest_item.text if latest_item else ""

        # Cache the result
        set_cached_user_data(username, "clipboard_latest", text)

        if latest_item:
            return JSONResponse(content={"status": "success", "text": text})
        return JSONResponse(content={"status": "success", "text": ""})
    except Exception as e:
        print(f"Error fetching latest clipboard text for {username}: {e}")
        return JSONResponse(
            content={
                "status": "error",
                "message": "Error fetching latest clipboard text",
            },
            status_code=500,
        )
    finally:
        db.close()


# API endpoint to submit new copied text (used by the desktop app)
@app.post("/api/submit_copied_text/{username}")
async def submit_copied_text(username: str, item: HistoryItem):
    db = SessionLocal()
    try:
        db.execute(
            copied_text_history.insert().values(username=username, text=item.text)
        )
        db.commit()

        # Invalidate copied text cache
        invalidate_user_cache(username, "copied_text_history")

        # Enforce max 10 copied text items
        items = db.execute(
            copied_text_history.select()
            .where(copied_text_history.c.username == username)
            .order_by(copied_text_history.c.id)
        ).fetchall()
        if len(items) > 10:
            items_to_delete = len(items) - 10
            db.execute(
                copied_text_history.delete()
                .where(copied_text_history.c.username == username)
                .where(
                    copied_text_history.c.id.in_(
                        [item.id for item in items[:items_to_delete]]
                    )
                )
            )
            db.commit()
        return JSONResponse(
            content={"status": "success", "message": "Copied text submitted"}
        )
    except Exception as e:
        print(f"Error submitting copied text for {username}: {e}")
        return JSONResponse(
            content={"status": "error", "message": "Error submitting data"},
            status_code=500,
        )
    finally:
        db.close()


# API endpoint to delete a copied text item
@app.post("/api/delete_copied_text/{username}")
async def delete_copied_text(username: str, item: HistoryItem, request: Request):
    if "user" not in request.session or request.session["user"]["username"] != username:
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            copied_text_history.delete()
            .where(copied_text_history.c.username == username)
            .where(copied_text_history.c.text == item.text)
        )
        db.commit()
        # Invalidate copied text cache
        invalidate_user_cache(username, "copied_text_history")

        return JSONResponse(
            content={"status": "success", "message": "Copied text item deleted"}
        )
    except Exception as e:
        print(f"Error deleting copied text for {username}: {e}")
        return JSONResponse(
            content={"status": "error", "message": "Error deleting copied text"},
            status_code=500,
        )
    finally:
        db.close()


# API endpoint to clear copied text
@app.post("/api/clear_copied_text/{username}")
async def clear_copied_text(username: str, request: Request):
    if "user" not in request.session or request.session["user"]["username"] != username:
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            copied_text_history.delete().where(
                copied_text_history.c.username == username
            )
        )
        db.commit()

        # Invalidate copied text cache
        invalidate_user_cache(username, "copied_text_history")

        return JSONResponse(
            content={"status": "success", "message": "Copied text history cleared"}
        )
    except Exception as e:
        print(f"Error clearing copied text for {username}: {e}")
        return JSONResponse(
            content={"status": "error", "message": "Error clearing copied text"},
            status_code=500,
        )
    finally:
        db.close()


# API endpoint to fetch submitted text history (Clipboard Manager)
@app.get("/api/submitted_text_history/{username}")
async def get_submitted_text_history(username: str, request: Request):
    if "user" not in request.session or request.session["user"]["username"] != username:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Try to get from cache first
    cached_data = get_cached_user_data(username, "submitted_text_history")
    if cached_data is not None:
        print(f"Returning cached submitted text history for {username}")
        return JSONResponse(
            content={
                "status": "success",
                "submitted_text_history": cached_data,
            }
        )

    db = SessionLocal()
    try:
        submitted_text_items = db.execute(
            submitted_text_history.select()
            .where(submitted_text_history.c.username == username)
            .order_by(submitted_text_history.c.id.desc())
        ).fetchall()

        # Cache the result
        history_list = [item.text for item in submitted_text_items]
        set_cached_user_data(username, "submitted_text_history", history_list)

        return JSONResponse(
            content={
                "status": "success",
                "submitted_text_history": history_list,
            }
        )
    except Exception as e:
        print(f"Error fetching submitted text history for {username}: {e}")
        return JSONResponse(
            content={
                "status": "error",
                "message": "Error fetching submitted text history",
            },
            status_code=500,
        )
    finally:
        db.close()


# API endpoint to submit new submitted text (Clipboard Manager history)
@app.post("/api/submit_submitted_text/{username}")
async def submit_submitted_text(username: str, item: HistoryItem, request: Request):
    if "user" not in request.session or request.session["user"]["username"] != username:
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            submitted_text_history.insert().values(username=username, text=item.text)
        )
        db.commit()

        # Invalidate submitted text cache
        invalidate_user_cache(username, "submitted_text_history")

        # Enforce max 10 submitted text items
        items = db.execute(
            submitted_text_history.select()
            .where(submitted_text_history.c.username == username)
            .order_by(submitted_text_history.c.id)
        ).fetchall()
        if len(items) > 10:
            items_to_delete = len(items) - 10
            db.execute(
                submitted_text_history.delete()
                .where(submitted_text_history.c.username == username)
                .where(
                    submitted_text_history.c.id.in_(
                        [item.id for item in items[:items_to_delete]]
                    )
                )
            )
            db.commit()
        return JSONResponse(
            content={"status": "success", "message": "Submitted text saved"}
        )
    except Exception as e:
        print(f"Error submitting submitted text for {username}: {e}")
        return JSONResponse(
            content={"status": "error", "message": "Error submitting submitted text"},
            status_code=500,
        )
    finally:
        db.close()


# API endpoint to delete a submitted text item
@app.post("/api/delete_submitted_text/{username}")
async def delete_submitted_text(username: str, item: HistoryItem, request: Request):
    if "user" not in request.session or request.session["user"]["username"] != username:
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            submitted_text_history.delete()
            .where(submitted_text_history.c.username == username)
            .where(submitted_text_history.c.text == item.text)
        )
        db.commit()

        # Invalidate submitted text cache
        invalidate_user_cache(username, "submitted_text_history")

        return JSONResponse(
            content={"status": "success", "message": "Submitted text item deleted"}
        )
    except Exception as e:
        print(f"Error deleting submitted text for {username}: {e}")
        return JSONResponse(
            content={"status": "error", "message": "Error deleting submitted text"},
            status_code=500,
        )
    finally:
        db.close()


# API endpoint to clear submitted text history
@app.post("/api/clear_submitted_text/{username}")
async def clear_submitted_text(username: str, request: Request):
    if "user" not in request.session or request.session["user"]["username"] != username:
        raise HTTPException(status_code=403, detail="Not authorized")
    db = SessionLocal()
    try:
        db.execute(
            submitted_text_history.delete().where(
                submitted_text_history.c.username == username
            )
        )
        db.commit()

        # Invalidate submitted text cache
        invalidate_user_cache(username, "submitted_text_history")

        return JSONResponse(
            content={"status": "success", "message": "Submitted text history cleared"}
        )
    except Exception as e:
        print(f"Error clearing submitted text for {username}: {e}")
        return JSONResponse(
            content={"status": "error", "message": "Error clearing submitted text"},
            status_code=500,
        )
    finally:
        db.close()


# Helper function to get all users for admin dashboard
def get_all_users(db):
    return db.execute(users.select()).fetchall()


# API endpoint to get cache statistics (for monitoring)
@app.get("/api/cache/stats")
async def get_cache_stats():
    """Return cache statistics for monitoring"""
    cache_stats = {
        "total_cached_users": len(user_data_cache),
        "cache_ttl_seconds": CACHE_TTL,
        "users_in_cache": list(user_data_cache.keys()),
        "cache_details": {},
    }

    # Get details for each cached user
    for username, cache_data in user_data_cache.items():
        cache_age = time.time() - cache_data.get("last_updated", 0)
        cache_stats["cache_details"][username] = {
            "cached_items": list(cache_data.keys()),
            "age_seconds": round(cache_age, 2),
            "expires_in_seconds": round(max(0, CACHE_TTL - cache_age), 2),
            "is_valid": cache_age < CACHE_TTL,
        }

    return JSONResponse(content={"status": "success", "cache_stats": cache_stats})


# API endpoint to clear all cache (admin only)
@app.post("/api/cache/clear")
async def clear_cache(request: Request):
    """Clear all cache - useful for debugging"""
    if request.session.get("user", {}).get("role") != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    user_data_cache.clear()
    login_attempts_cache.clear()

    return JSONResponse(
        content={
            "status": "success",
            "message": "All cache cleared successfully",
        }
    )
