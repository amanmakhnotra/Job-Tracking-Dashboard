"""Local job dashboard with Gmail read-only refresh and SQLite storage."""
import base64
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_from_directory, session
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

load_dotenv()
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DATABASE = DATA_DIR / "dashboard.db"
SECRETS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:5000/auth/google/callback")
SEED_VERSION = "2026-08-27-curated-v2"

app = Flask(__name__, static_folder="public")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "fallback-secret-key-change-in-env")


def db():
    # A refresh can make several Gmail requests. Allow SQLite to wait briefly
    # if the browser makes another local request while a write is completing.
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.row_factory = sqlite3.Row
    return connection


def setup_database():
    DATA_DIR.mkdir(exist_ok=True)
    with db() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY,
                company TEXT NOT NULL,
                role TEXT NOT NULL,
                applied TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'assessment', 'rejected')),
                label TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        seed_version = connection.execute("SELECT value FROM settings WHERE name = 'seed_version'").fetchone()
        # Version 2 restores the curated baseline after the earlier broad Gmail
        # matching rule incorrectly overwrote several application statuses.
        if (not seed_version or seed_version["value"] != SEED_VERSION) and (DATA_DIR / "applications.json").exists():
            seed = json.loads((DATA_DIR / "applications.json").read_text(encoding="utf-8"))
            connection.execute("DELETE FROM applications")
            connection.executemany(
                "INSERT INTO applications (company, role, applied, status, label) VALUES (?, ?, ?, ?, ?)",
                [(item["company"], item["role"], item["applied"], item["status"], item.get("label")) for item in seed],
            )
            connection.execute("INSERT OR REPLACE INTO settings (name, value) VALUES ('seed_version', ?)", (SEED_VERSION,))


def configured():
    return all(os.getenv(key) for key in SECRETS)


def oauth_flow():
    return Flow.from_client_config({
        "web": {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }, scopes=SCOPES, redirect_uri=REDIRECT_URI)


def save_token(credentials):
    value = credentials.to_json()
    with db() as connection:
        connection.execute("INSERT OR REPLACE INTO settings (name, value) VALUES ('gmail_token', ?)", (value,))


def credentials():
    with db() as connection:
        row = connection.execute("SELECT value FROM settings WHERE name = 'gmail_token'").fetchone()
    if not row:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(row["value"]), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_token(creds)
    return creds if creds.valid else None


def classify(text):
    text = text.lower()
    # Expanded rejection phrasing
    if re.search(r"unfortunately|not (?:be )?moving forward|not selected|no longer under consideration|decided to move forward with other|regret to inform|application.*(?:unsuccessful|rejected)|thank you for your interest.*pursuing other", text):
        return "rejected", "Not selected"
    if re.search(r"online assessment|assessment (?:invite|link)|complete (?:the )?(?:test|assessment)|coding challenge|interview (?:invite|schedule|invitation)|schedule.*interview|next (?:round|step)|action (?:needed|required)", text):
        return "assessment", "Assessment / action"
    return None


def message_text(message):
    headers = message.get("payload", {}).get("headers", [])
    subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "")
    parts = []

    def visit(part):
        body = part.get("body", {}).get("data")
        if body:
            parts.append(base64.urlsafe_b64decode(body + "==").decode("utf-8", errors="replace"))
        for child in part.get("parts", []):
            visit(child)

    visit(message.get("payload", {}))
    return re.sub(r"<[^>]*>", " ", subject + "\n" + "\n".join(parts))


def application_list():
    with db() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM applications ORDER BY applied DESC")]


def role_terms(role):
    """Return distinctive words that can tie an update to one specific application."""
    ignored = {"analyst", "associate", "manager", "program", "project", "senior", "junior", "role", "and", "the", "for", "via"}
    return [word for word in re.findall(r"[a-z]{4,}", role.lower()) if word not in ignored]


def matches_application(text, application):
    """Avoid applying one company's email to every role at that company."""
    value = text.lower()
    company_words = [word for word in re.findall(r"[a-z]{3,}", application["company"].lower()) if word not in {"and", "the"}]
    company_match = application["company"].lower() in value or any(word in value for word in company_words)
    terms = role_terms(application["role"])
    role_match = bool(terms) and (sum(term in value for term in terms) >= min(2, len(terms)))
    return company_match and role_match


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def state():
    return jsonify(configured=configured(), connected=bool(credentials()), applications=application_list())

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
@app.get("/auth/google")
def connect_gmail():
    if not configured():
        return "Create .env first; see README.md.", 400
    
    flow = oauth_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true"
    )
    
    # Save both state and code_verifier generated by the flow
    session["oauth_state"] = state
    session["code_verifier"] = flow.code_verifier
    
    return redirect(url)


@app.get("/auth/google/callback")
def gmail_callback():
    try:
        # Recreate flow passing state from session
        state = session.get("oauth_state")
        flow = oauth_flow()
        if state:
            flow.state = state

        # Retrieve the code_verifier stored during authorization
        code_verifier = session.get("code_verifier")

        # Fetch token passing the saved code_verifier
        flow.fetch_token(
            code=request.args["code"],
            code_verifier=code_verifier
        )

        save_token(flow.credentials)
        return redirect("/?connected=1")
    except Exception as error:
        app.logger.exception("Gmail OAuth callback failed: %s", error)
        return "Gmail connection failed. Check the VS Code terminal for the exact error, then try again.", 500


@app.post("/api/refresh")
def refresh():
    creds = credentials()
    if not creds:
        return jsonify(error="Connect Gmail first."), 401
    try:
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        changes = 0
        checked_messages = 0
        # Query Gmail once for likely status emails rather than searching every
        # company separately. This is faster and avoids broad company-name matches.
        query = 'newer_than:60d ("online assessment" OR "coding challenge" OR "not moving forward" OR "not selected" OR "unfortunately" OR "next step" OR "action required")'
        messages = gmail.users().messages().list(userId="me", q=query, maxResults=100).execute().get("messages", [])
        candidates = []
        for result in messages:
            msg = gmail.users().messages().get(userId="me", id=result["id"], format="full").execute()
            status_info = classify(message_text(msg))
            if status_info:
                candidates.append((int(msg["internalDate"]), message_text(msg), status_info))
            checked_messages += 1
        with db() as connection:
            applications = connection.execute("SELECT * FROM applications").fetchall()
            for application in applications:
                newest = None
                for sent_at, text, status_info in candidates:
                    if matches_application(text, application) and (not newest or sent_at > newest[0]):
                        newest = (sent_at, *status_info)
                if newest and application["status"] != newest[1]:
                    connection.execute(
                        "UPDATE applications SET status=?, label=?, updated_at=? WHERE id=?",
                        (newest[1], newest[2], datetime.fromtimestamp(newest[0] / 1000, timezone.utc).isoformat(), application["id"])
                    )
                    changes += 1
        return jsonify(applications=application_list(), changes=changes, checkedMessages=checked_messages, refreshedAt=datetime.now(timezone.utc).isoformat())
    except Exception as error:
        app.logger.exception("Gmail refresh failed: %s", error)
        return jsonify(error="Gmail fetch failed."), 500
    
if __name__ == "__main__":
    setup_database()
    # One local server process avoids SQLite write contention from the reloader.
    app.run(port=int(os.getenv("PORT", "5000")), debug=False)
