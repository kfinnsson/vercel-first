import os
from datetime import datetime, timezone

import psycopg2
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from mangum import Mangum
from pydantic import BaseModel

app = FastAPI()

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Guestbook</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, -apple-system, sans-serif;
      max-width: 600px;
      margin: 2rem auto;
      padding: 0 1rem;
      background: #f9fafb;
      color: #1f2937;
    }
    h1 { text-align: center; margin-bottom: 1.5rem; }
    form {
      background: #fff;
      padding: 1.5rem;
      border-radius: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1);
      margin-bottom: 2rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }
    input, textarea {
      padding: 0.5rem 0.75rem;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      font-size: 1rem;
    }
    textarea { resize: vertical; min-height: 80px; }
    button {
      padding: 0.6rem 1.25rem;
      background: #2563eb;
      color: #fff;
      border: none;
      border-radius: 6px;
      font-size: 1rem;
      cursor: pointer;
    }
    button:hover { background: #1d4ed8; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    .entry {
      background: #fff;
      padding: 1rem 1.25rem;
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(0,0,0,0.06);
      margin-bottom: 0.75rem;
    }
    .entry-header {
      display: flex;
      justify-content: space-between;
      margin-bottom: 0.25rem;
    }
    .entry-name { font-weight: 600; }
    .entry-date { color: #6b7280; font-size: 0.85rem; }
    .entry-message { line-height: 1.5; }
    #status { text-align: center; color: #6b7280; padding: 1rem; }
  </style>
</head>
<body>
  <h1>📝 Guestbook</h1>

  <form id="form">
    <input type="text" id="name" placeholder="Your name" required maxlength="100" />
    <textarea id="message" placeholder="Leave a message..." required></textarea>
    <button type="submit">Sign Guestbook</button>
  </form>

  <div id="entries"></div>
  <div id="status">Loading messages...</div>

  <script>
    const form = document.getElementById("form");
    const entriesDiv = document.getElementById("entries");
    const statusDiv = document.getElementById("status");

    function renderEntry(e) {
      const date = new Date(e.created_at).toLocaleString();
      return `<div class="entry">
        <div class="entry-header">
          <span class="entry-name">${esc(e.name)}</span>
          <span class="entry-date">${date}</span>
        </div>
        <div class="entry-message">${esc(e.message)}</div>
      </div>`;
    }

    function esc(s) {
      const d = document.createElement("div");
      d.textContent = s;
      return d.innerHTML;
    }

    async function loadMessages() {
      try {
        const res = await fetch("/api/messages");
        const data = await res.json();
        if (data.length === 0) {
          statusDiv.textContent = "No messages yet. Be the first to sign!";
        } else {
          statusDiv.textContent = "";
          entriesDiv.innerHTML = data.map(renderEntry).join("");
        }
      } catch {
        statusDiv.textContent = "Failed to load messages.";
      }
    }

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = form.querySelector("button");
      btn.disabled = true;
      try {
        const res = await fetch("/api/messages", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: document.getElementById("name").value,
            message: document.getElementById("message").value,
          }),
        });
        if (!res.ok) throw new Error("Failed");
        form.reset();
        await loadMessages();
      } catch {
        alert("Could not submit message. Please try again.");
      } finally {
        btn.disabled = false;
      }
    });

    loadMessages();
  </script>
</body>
</html>"""

DATABASE_URL = os.environ.get("DATABASE_URL", "")


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


@app.post("/api/messages", status_code=201)
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
    return HTMLResponse(INDEX_HTML)


handler = Mangum(app)
