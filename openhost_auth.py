"""OpenHost federated auth proxy for Plane.

Sits inside the Plane container alongside the other services. Handles:
- Owner adding guest users by OpenHost domain
- Guest login via the OpenHost identity challenge flow
- Creating Plane user accounts and Django sessions directly in PostgreSQL

Routes (served via Caddy at /openhost-auth/*):
- GET  /manage          — owner UI to add/remove guest users
- POST /add-user        — owner adds a guest by domain
- POST /remove-user     — owner removes a guest
- GET  /login           — guest clicks to start identity login
- GET  /callback        — receives signed identity_token, creates Plane session
"""

import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from django.conf import settings as django_settings
django_settings.configure(SECRET_KEY="unused", USE_TZ=True)

import jwt
import psycopg2
import psycopg2.extras
import requests
from flask import Flask, Response, redirect, request, jsonify, render_template_string

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/plane")
ZONE_DOMAIN = os.environ.get("OPENHOST_ZONE_DOMAIN", "localhost")
ROUTER_URL = os.environ.get("OPENHOST_ROUTER_URL", "http://127.0.0.1:8080")
# The external URL guests use to reach this Plane instance
EXTERNAL_URL = os.environ.get("WEB_URL", f"http://{ZONE_DOMAIN}")
DJANGO_SECRET_KEY = os.environ.get("SECRET_KEY", "")
OWNER_EMAIL = "owner@openhost.local"

# File to persist known guest identities (domain -> public_key_pem)
GUESTS_FILE = os.path.join(os.environ.get("OPENHOST_APP_DATA_DIR", "/app/data"), "openhost_guests.json")


def _db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _load_guests():
    """Load guest identity records from disk."""
    if os.path.exists(GUESTS_FILE):
        with open(GUESTS_FILE) as f:
            return json.load(f)
    return {}


def _save_guests(guests):
    os.makedirs(os.path.dirname(GUESTS_FILE), exist_ok=True)
    with open(GUESTS_FILE, "w") as f:
        json.dump(guests, f, indent=2)


def _setup_instance():
    """Auto-setup Plane instance on first boot: create admin user + mark done."""
    log.info("Instance setup: waiting for database and migrations...")
    instance = None
    for attempt in range(600):
        try:
            conn = _db()
            cur = conn.cursor()
            cur.execute("SELECT id, is_setup_done FROM instances LIMIT 1")
            instance = cur.fetchone()
            conn.close()
            if instance:
                log.info("Instance setup: found instance record after %d seconds", attempt)
                break
        except Exception as e:
            if attempt % 10 == 0:
                log.info("Instance setup: waiting for database... (%ds, %s)", attempt, e)
            pass
        time.sleep(1)

    if not instance:
        log.error("Instance setup: timed out waiting for database after 600s")
        return

    if instance["is_setup_done"]:
        log.info("Instance already set up, skipping auto-setup")
        return

    instance_id = instance["id"]
    now = datetime.now(timezone.utc)
    conn = _db()
    try:
        cur = conn.cursor()

        # Create or find owner user
        cur.execute("SELECT id FROM users WHERE email = %s", (OWNER_EMAIL,))
        existing = cur.fetchone()
        if existing:
            user_id = existing["id"]
        else:
            user_id = uuid.uuid4()
            cur.execute(
                """INSERT INTO users (
                    id, created_at, updated_at,
                    password, last_login,
                    is_superuser, username, first_name, last_name,
                    email, is_staff, is_active, date_joined,
                    is_managed, is_password_autoset, is_email_verified,
                    is_password_expired, is_bot, display_name,
                    avatar, is_email_valid,
                    last_location, created_location, token,
                    user_timezone, last_login_ip, last_logout_ip,
                    last_login_medium, last_login_uagent
                ) VALUES (
                    %s, %s, %s,
                    '', NULL,
                    true, %s, 'Owner', '',
                    %s, true, true, %s,
                    false, true, true,
                    false, false, 'Owner',
                    '', true,
                    '', '', '',
                    'UTC', '', '',
                    '', ''
                )""",
                (str(user_id), now, now, "owner", OWNER_EMAIL, now),
            )

        # Create instance admin
        cur.execute(
            "SELECT id FROM instance_admins WHERE user_id = %s",
            (str(user_id),),
        )
        if not cur.fetchone():
            cur.execute(
                """INSERT INTO instance_admins
                   (id, created_at, updated_at, role, is_verified, user_id, instance_id)
                   VALUES (%s, %s, %s, 20, true, %s, %s)""",
                (str(uuid.uuid4()), now, now, str(user_id), str(instance_id)),
            )

        # Mark instance setup as done
        cur.execute(
            "UPDATE instances SET is_setup_done = true, is_signup_screen_visited = true WHERE id = %s",
            (str(instance_id),),
        )

        conn.commit()
        log.info("Instance auto-setup complete: admin user %s", user_id)
    except Exception as e:
        log.error("Instance setup failed: %s", e)
        conn.rollback()
    finally:
        conn.close()


def _is_owner(req):
    """Check if the request is from the zone owner.

    The OpenHost router sets X-OpenHost-Is-Owner: true for authenticated
    owner requests. The header is always overwritten by the router so it
    cannot be spoofed by external clients.
    """
    return req.headers.get("X-OpenHost-Is-Owner") == "true"


def _fetch_identity(domain):
    """Fetch a remote zone's public identity."""
    for proto in ("https", "http"):
        try:
            resp = requests.get(
                f"{proto}://{domain}/.well-known/openhost-identity", timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            continue
    return None


def _find_or_create_plane_user(domain):
    """Find or create a Plane user account for an OpenHost guest.

    Returns the Plane user ID (UUID).
    """
    # Use the domain as a stable email-like identifier
    email = f"{domain.replace('.', '-')}@openhost.guest"
    username = domain.replace(".", "-")

    conn = _db()
    try:
        cur = conn.cursor()

        # Check if user already exists
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        if row:
            return row["id"]

        # Create user in Plane's users table
        user_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        cur.execute(
            """INSERT INTO users (
                id, created_at, updated_at,
                password, last_login,
                is_superuser, username, first_name, last_name,
                email, is_staff, is_active, date_joined,
                is_managed, is_password_autoset, is_email_verified,
                is_password_expired, is_bot, display_name,
                avatar, is_email_valid,
                last_location, created_location, token,
                user_timezone, last_login_ip, last_logout_ip,
                last_login_medium, last_login_uagent
            ) VALUES (
                %s, %s, %s,
                '', NULL,
                false, %s, %s, '',
                %s, false, true, %s,
                false, true, true,
                false, false, %s,
                '', true,
                '', '', '',
                'UTC', '', '',
                '', ''
            )""",
            (str(user_id), now, now, username, domain, email, now, domain),
        )
        conn.commit()
        log.info("Created Plane user %s for domain %s", user_id, domain)
        return user_id
    finally:
        conn.close()


def _create_django_session(user_id):
    """Create a Django session for a Plane user.

    Returns (session_key, expiry_datetime).
    Plane uses Django's db-backed sessions (django_session table).
    """
    session_key = secrets.token_hex(16)
    expire_date = datetime.now(timezone.utc) + timedelta(days=14)

    # Use Django's signing module to create a properly encoded session
    from django.core import signing

    session_data = {
        "_auth_user_id": str(user_id),
        "_auth_user_backend": "django.contrib.auth.backends.ModelBackend",
        "_auth_user_hash": "",
    }

    encoded = signing.dumps(
        session_data,
        key=DJANGO_SECRET_KEY,
        salt="django.contrib.sessions.backends.db",
        serializer=signing.JSONSerializer,
    )

    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO django_session (session_key, session_data, expire_date)
               VALUES (%s, %s, %s)
               ON CONFLICT (session_key) DO UPDATE
               SET session_data = EXCLUDED.session_data, expire_date = EXCLUDED.expire_date""",
            (session_key, encoded, expire_date),
        )
        conn.commit()
        return session_key, expire_date
    finally:
        conn.close()


# ─── Routes ───


@app.route("/manage")
def manage_users():
    """Owner UI to manage guest users."""
    if not _is_owner(request):
        return redirect(f"{EXTERNAL_URL}")

    guests = _load_guests()
    return render_template_string(MANAGE_TEMPLATE, guests=guests, zone_domain=ZONE_DOMAIN)


@app.route("/add-user", methods=["POST"])
def add_user():
    """Owner adds a guest user by their OpenHost domain."""
    if not _is_owner(request):
        return Response("Unauthorized", status=401)

    domain = request.form.get("domain", "").strip().lower()
    if not domain:
        return Response("Missing domain", status=400)

    # Fetch their public identity
    identity = _fetch_identity(domain)
    if not identity:
        return Response(f"Could not fetch identity from {domain}", status=400)

    # Create Plane account
    user_id = _find_or_create_plane_user(domain)

    # Store guest identity
    guests = _load_guests()
    guests[domain] = {
        "public_key_pem": identity["public_key_pem"],
        "plane_user_id": str(user_id),
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_guests(guests)

    log.info("Added guest %s (plane user %s)", domain, user_id)
    return redirect("/openhost-auth/manage")


@app.route("/remove-user", methods=["POST"])
def remove_user():
    """Owner removes a guest user."""
    if not _is_owner(request):
        return Response("Unauthorized", status=401)

    domain = request.form.get("domain", "").strip().lower()
    guests = _load_guests()
    if domain in guests:
        del guests[domain]
        _save_guests(guests)
    return redirect("/openhost-auth/manage")


@app.route("/login")
def login():
    """Guest initiates identity login.

    If ?domain= is provided, redirect directly to that zone's approve endpoint.
    Otherwise, show a form to enter their domain.
    """
    domain = request.args.get("domain", "").strip().lower()

    if not domain:
        return render_template_string(LOGIN_TEMPLATE, error=None)

    # Check this domain is a known guest
    guests = _load_guests()
    if domain not in guests:
        return render_template_string(LOGIN_TEMPLATE, error=f"{domain} is not authorized to access this app")

    # Build callback URL
    callback = f"{EXTERNAL_URL}/openhost-auth/callback"

    # Redirect to guest's zone for identity approval
    params = urlencode({
        "callback": callback,
        "app_name": "Plane",
        "requesting_domain": ZONE_DOMAIN,
    })

    # Try HTTPS first
    approve_url = f"https://{domain}/identity/approve?{params}"
    return redirect(approve_url)


@app.route("/callback")
def callback():
    """Receive signed identity token from guest's zone, create Plane session."""
    identity_token = request.args.get("identity_token", "")
    if not identity_token:
        return Response("Missing identity_token", status=400)

    # Decode without verification first to get the domain
    try:
        unverified = jwt.decode(identity_token, options={"verify_signature": False})
    except Exception:
        return Response("Invalid token", status=400)

    domain = unverified.get("sub", "")
    guests = _load_guests()

    if domain not in guests:
        return Response(f"Unknown identity: {domain}", status=403)

    # Verify the token with the guest's public key
    guest = guests[domain]
    try:
        claims = jwt.decode(
            identity_token,
            guest["public_key_pem"],
            algorithms=["RS256"],
            audience=f"{EXTERNAL_URL}/openhost-auth/callback",
        )
    except jwt.ExpiredSignatureError:
        return Response("Identity token expired", status=401)
    except jwt.InvalidAudienceError:
        return Response("Token audience mismatch", status=401)
    except Exception as e:
        return Response(f"Token verification failed: {e}", status=401)

    # Token is valid — find or create the Plane user and create a session
    user_id = _find_or_create_plane_user(domain)
    session_key, expire_date = _create_django_session(user_id)

    # Set the Django session cookie and redirect to Plane
    resp = redirect(f"{EXTERNAL_URL}/")
    resp.set_cookie(
        "sessionid",
        session_key,
        expires=expire_date,
        path="/",
        httponly=True,
        samesite="Lax",
    )
    log.info("Logged in guest %s as Plane user %s", domain, user_id)
    return resp


@app.route("/check-session")
def check_session():
    """Forward-auth endpoint: auto-login zone owner into Plane."""
    # Fast path: already has a valid Plane session
    existing_session = request.cookies.get("sessionid")
    if existing_session:
        try:
            conn = _db()
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM django_session WHERE session_key = %s AND expire_date > NOW()",
                (existing_session,),
            )
            if cur.fetchone():
                log.info("check-session: valid existing session")
                return Response("ok", status=200)
            conn.close()
        except Exception as e:
            log.warning("check-session: error validating session: %s", e)
        log.info("check-session: stale session cookie, will re-create")

    is_owner = _is_owner(request)
    log.info("check-session: is_owner=%s, has_session=%s", is_owner, bool(existing_session))

    # Check if this is the zone owner
    if not is_owner:
        return Response("ok", status=200)

    # Owner without valid session — create one
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = %s", (OWNER_EMAIL,))
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        log.warning("check-session: owner user %s not found in DB", OWNER_EMAIL)
        return Response("ok", status=200)

    user_id = row["id"]
    session_key, expire_date = _create_django_session(user_id)
    log.info("check-session: created session for owner (user %s)", user_id)

    # Redirect back to the original URL with the new session cookie
    original_uri = request.headers.get("X-Forwarded-Uri", "/")
    resp = redirect(original_uri)
    resp.set_cookie(
        "sessionid", session_key,
        expires=expire_date, path="/",
        httponly=True, samesite="Lax",
    )
    log.info("Auto-logged in zone owner")
    return resp


# ─── Templates ───

MANAGE_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
  <title>Manage Guest Users - Plane</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; max-width: 600px; margin: 2em auto; padding: 0 1em; }
    table { width: 100%; border-collapse: collapse; margin: 1em 0; }
    th, td { text-align: left; padding: 0.5em; border-bottom: 1px solid #eee; }
    .add-form { margin: 1.5em 0; display: flex; gap: 0.5em; }
    .add-form input[type=text] { flex: 1; padding: 0.4em; }
    button { padding: 0.4em 1em; border-radius: 4px; border: 1px solid #ccc; cursor: pointer; }
    .add-btn { background: #2563eb; color: white; border-color: #2563eb; }
    .remove-btn { background: white; color: #c00; border-color: #c00; font-size: 0.85em; }
    .back { display: inline-block; margin-top: 1em; color: #666; }
  </style>
</head>
<body>
  <h2>Guest Users</h2>
  <p>Add OpenHost users by their domain so they can access this Plane instance.</p>

  <form method="post" action="/openhost-auth/add-user" class="add-form">
    <input type="text" name="domain" placeholder="e.g. bob.host.imbue.com" required>
    <button type="submit" class="add-btn">Add User</button>
  </form>

  {% if guests %}
  <table>
    <tr><th>Domain</th><th>Added</th><th></th></tr>
    {% for domain, info in guests.items() %}
    <tr>
      <td>{{ domain }}</td>
      <td>{{ info.get('added_at', '')[:10] }}</td>
      <td>
        <form method="post" action="/openhost-auth/remove-user" style="display:inline">
          <input type="hidden" name="domain" value="{{ domain }}">
          <button type="submit" class="remove-btn">Remove</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p style="color:#888">No guest users added yet.</p>
  {% endif %}

  <a class="back" href="/">&larr; Back to Plane</a>
</body>
</html>
"""

LOGIN_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
  <title>Login with OpenHost Identity</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; max-width: 400px; margin: 4em auto; padding: 0 1em; }
    input[type=text] { display: block; margin: 0.5em 0; padding: 0.4em; width: 100%; box-sizing: border-box; }
    .error { color: #c00; }
    button { padding: 0.5em 1.5em; background: #2563eb; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1em; }
    button:hover { background: #1d4ed8; }
  </style>
</head>
<body>
  <h2>Login with OpenHost Identity</h2>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <p>Enter your OpenHost domain to log in:</p>
  <form method="get" action="/openhost-auth/login">
    <input type="text" name="domain" placeholder="e.g. alice.host.imbue.com" required>
    <button type="submit">Login</button>
  </form>
</body>
</html>
"""


if __name__ == "__main__":
    _setup_instance()
    app.run(host="0.0.0.0", port=3006)
