import os
import hmac
import hashlib
import time
from pathlib import Path
from datetime import datetime, timezone

import psycopg2
from fastapi import FastAPI, HTTPException, Request, Depends, Cookie
from fastapi.responses import HTMLResponse
from mangum import Mangum
from pydantic import BaseModel

app = FastAPI(redoc_url=None)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")


def sign_token(timestamp: str) -> str:
    return hmac.new(SECRET_KEY.encode(), timestamp.encode(), hashlib.sha256).hexdigest()


def require_session(session_token: str = Cookie(None)):
    """Validate the signed session cookie."""
    if not session_token or not SECRET_KEY:
        raise HTTPException(status_code=401, detail="Not authenticated")
    parts = session_token.split(".")
    if len(parts) != 2:
        raise HTTPException(status_code=401, detail="Not authenticated")
    timestamp, signature = parts
    if not hmac.compare_digest(sign_token(timestamp), signature):
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Reject tokens older than 24 hours
    try:
        if time.time() - float(timestamp) > 86400:
            raise HTTPException(status_code=401, detail="Session expired")
    except ValueError:
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


@app.get("/api/messages", dependencies=[Depends(require_session)])
def list_messages():
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


@app.post("/api/messages", status_code=201, dependencies=[Depends(require_session)])
def create_message(msg: MessageIn):
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


@app.get("/", response_class=HTMLResponse)
def serve_index():
    html_path = Path(__file__).parent / "index.html"
    html = html_path.read_text()
    timestamp = str(int(time.time()))
    token = f"{timestamp}.{sign_token(timestamp)}"
    response = HTMLResponse(html)
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=86400,
    )
    return response


handler = Mangum(app)
