import os
import hmac
import hashlib
import json
import time
import secrets
import base64
from pathlib import Path
from datetime import datetime, timezone

import psycopg2
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request, Depends, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from mangum import Mangum
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI(redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "dev"))

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")

# --- OAuth setup ---
oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

SESSION_MAX_AGE = 86400  # 24 hours


def _sign(payload: bytes) -> str:
    return hmac.new(SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def _encode_session(data: dict) -> str:
    """Create a signed session cookie value: base64url(json).signature"""
    payload = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
    sig = _sign(payload)
    return payload.decode() + "." + sig


def _decode_session(token: str) -> dict:
    """Validate and decode a signed session cookie. Raises on failure."""
    parts = token.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError("bad format")
    payload_b64, sig = parts
    if not hmac.compare_digest(_sign(payload_b64.encode()), sig):
        raise ValueError("bad signature")
    data = json.loads(base64.urlsafe_b64decode(payload_b64))
    if time.time() - data.get("ts", 0) > SESSION_MAX_AGE:
        raise ValueError("expired")
    return data


def require_session(session_token: str = Cookie(None)):
    """Validate the signed session cookie and return user info."""
    if not session_token or not SECRET_KEY:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return _decode_session(session_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Not authenticated")


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def ensure_table():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS guestbook (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    message TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.commit()


class MessageIn(BaseModel):
    name: str
    message: str


@app.on_event("startup")
def startup():
    if DATABASE_URL:
        ensure_table()


@app.get("/api/messages")
def list_messages(user: dict = Depends(require_session)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, message, created_at FROM guestbook ORDER BY created_at DESC LIMIT 100"
            )
            rows = cur.fetchall()
    return [
        {"id": r[0], "name": r[1], "message": r[2], "created_at": r[3].isoformat()}
        for r in rows
    ]


@app.post("/api/messages", status_code=201)
def create_message(msg: MessageIn, user: dict = Depends(require_session)):
    if not msg.name.strip() or not msg.message.strip():
        raise HTTPException(status_code=400, detail="Name and message are required")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO guestbook (name, message) VALUES (%s, %s) RETURNING id, created_at",
                (msg.name.strip(), msg.message.strip()),
            )
            row = cur.fetchone()
            conn.commit()
    return {"id": row[0], "name": msg.name.strip(), "message": msg.message.strip(), "created_at": row[1].isoformat()}


@app.get("/api/me")
def get_me(user: dict = Depends(require_session)):
    """Return current user info from the session cookie."""
    return {"email": user.get("email", ""), "name": user.get("name", "")}


# --- Auth routes ---

@app.get("/auth/login")
async def auth_login(request: Request):
    """Redirect to Google's consent screen."""
    state = secrets.token_urlsafe(32)
    redirect_uri = GOOGLE_REDIRECT_URI or str(request.url_for("auth_callback"))
    redirect = await oauth.google.authorize_redirect(request, redirect_uri, state=state)
    # Store state in a short-lived Lax cookie so it survives the cross-site redirect
    redirect.set_cookie(
        key="oauth_state",
        value=_encode_session({"state": state, "ts": int(time.time())}),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=300,
        path="/auth/callback",
    )
    return redirect


@app.get("/auth/callback")
async def auth_callback(request: Request, oauth_state: str = Cookie(None)):
    """Exchange the auth code for user info and set a session cookie."""
    # Validate OAuth state
    state_from_query = request.query_params.get("state", "")
    if not oauth_state:
        raise HTTPException(status_code=400, detail="Missing OAuth state cookie")
    try:
        state_data = _decode_session(oauth_state)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    if not hmac.compare_digest(state_data.get("state", ""), state_from_query):
        raise HTTPException(status_code=400, detail="State mismatch")

    # Exchange code for tokens and fetch user info
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo")
    if not userinfo:
        raise HTTPException(status_code=400, detail="Failed to get user info")

    # Build session cookie with user identity
    session_data = {
        "sub": userinfo.get("sub", ""),
        "email": userinfo.get("email", ""),
        "name": userinfo.get("name", ""),
        "ts": int(time.time()),
    }
    session_value = _encode_session(session_data)

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="session_token",
        value=session_value,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=SESSION_MAX_AGE,
    )
    # Clear the temporary OAuth state cookie
    response.delete_cookie(key="oauth_state", path="/auth/callback")
    return response


@app.get("/auth/logout")
def auth_logout():
    """Clear the session cookie and redirect to home."""
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie(key="session_token")
    return response


@app.get("/", response_class=HTMLResponse)
def serve_index():
    html_path = Path(__file__).parent / "index.html"
    html = html_path.read_text()
    return HTMLResponse(html)


handler = Mangum(app)
