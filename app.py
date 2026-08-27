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

app = Flask(__name__, static_folder="public")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "fallback-secret-key-change-in-env")


def db():
    connection = sqlite3.connect(DATABASE)
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
        count = connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        if count == 0 and (DATA_DIR / "applications.json").exists():
            seed = json.loads((DATA_DIR / "applications.json").read_text(encoding="utf-8"))
            connection.executemany(
                "INSERT INTO applications (company, role, applied, status, label) VALUES (?, ?, ?, ?, ?)",
                [(item["company"], item["role"], item["applied"], item["status"], item.get("label")) for item in seed],
            )


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
    if re.search(r"unfortunately|not (?:be )?moving forward|not selected|no longer under consideration|decided to move forward with other|regret to inform|application.*(?:unsuccessful|rejected)|thank you for your interest.*pursuing other|expired", text):
        return "rejected", "Not selected"
    # Expanded assessment / interview phrasing
    if re.search(r"assessment|online test|coding challenge|complete (?:the )?(?:test|assessment)|interview|schedule.*(?:call|interview)|next (?:round|step)|hacker rank|glider|shl|testgorilla|mock exam|excel/powerpoint", text):
        return "assessment", "Assessment / next stage"
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
        with db() as connection:
            applications = connection.execute("SELECT * FROM applications").fetchall()
            for application in applications:
                # Query base company string
                base_company = application["company"].split()[0]
                results = gmail.users().messages().list(
                    userId="me", q=f'"{base_company}"', maxResults=10
                ).execute().get("messages", [])
                
                newest = None
                for result in results:
                    msg = gmail.users().messages().get(userId="me", id=result["id"], format="full").execute()
                    text = message_text(msg)
                    status_info = classify(text)
                    if status_info and (not newest or int(msg["internalDate"]) > newest[0]):
                        newest = (int(msg["internalDate"]), *status_info)
                        
                if newest and application["status"] != newest[1]:
                    connection.execute(
                        "UPDATE applications SET status=?, label=?, updated_at=? WHERE id=?",
                        (newest[1], newest[2], datetime.fromtimestamp(newest[0] / 1000, timezone.utc).isoformat(), application["id"])
                    )
                    changes += 1
        return jsonify(applications=application_list(), changes=changes, refreshedAt=datetime.now(timezone.utc).isoformat())
    except Exception as error:
        app.logger.exception("Gmail refresh failed: %s", error)
        return jsonify(error="Gmail fetch failed."), 500
    
if __name__ == "__main__":
    setup_database()
    app.run(port=int(os.getenv("PORT", "5000")), debug=True)