import os
from pathlib import Path
from datetime import datetime, timezone

import psycopg2
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse
from mangum import Mangum
from pydantic import BaseModel

app = FastAPI(redoc_url=None)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
API_KEY = os.environ.get("API_KEY", "")


def require_api_key(x_api_key: str = Header()):
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


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


@app.get("/api/messages", dependencies=[Depends(require_api_key)])
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


@app.post("/api/messages", status_code=201, dependencies=[Depends(require_api_key)])
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
    html = html_path.read_text().replace("{{API_KEY}}", API_KEY)
    return HTMLResponse(html)


handler = Mangum(app)
