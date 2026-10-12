import os
import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from urllib.parse import quote

import pymysql
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for

from catalog_preload import load_catalog_preload
from local_store import ensure_local_schema, local_connection, sync_booking_date


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "change-this-for-production")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
app.logger.setLevel(logging.INFO)

# Build the read-only Bhasin profile/test catalog once when the Flask process starts.
CATALOG_PRELOAD = load_catalog_preload()

DELIVERY_LABELS = {
    "C": "Courier",
    "H": "By Hand",
}

CHARGE_MODE_LABELS = {
    "1": "Paying",
    "2": "Free",
    "3": "Credit",
}

PUBLIC_ENDPOINTS = {"login", "login_submit", "login_user_suggestions", "logout", "static"}
CENTRAL_USER_COLUMNS = None


def quote_identifier(value):
    """Safely quote a MySQL identifier discovered from metadata."""
    return "`" + str(value).replace("`", "``") + "`"


def first_existing_column(columns, candidates):
    for candidate in candidates:
        if candidate.lower() in columns:
            return columns[candidate.lower()]
    return None


def central_user_connection():
    """Connect to the central login database."""
    return pymysql.connect(
        host=os.getenv("CENTRAL_USER_DB_HOST", "10.1.1.67"),
        port=int(os.getenv("CENTRAL_USER_DB_PORT", "3306")),
        user=os.getenv("CENTRAL_USER_DB_USER", "root"),
        password=os.getenv("CENTRAL_USER_DB_PASSWORD", "arpra_root_pass"),
        database=os.getenv("CENTRAL_USER_DB_NAME", "central_user_database"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=8,
        read_timeout=20,
        write_timeout=20,
    )


def central_user_column_map():
    """Read and cache user-table columns so the login remains tolerant to naming."""
    global CENTRAL_USER_COLUMNS
    if CENTRAL_USER_COLUMNS is not None:
        return CENTRAL_USER_COLUMNS
    with central_user_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM `user`")
            CENTRAL_USER_COLUMNS = {row["Field"].lower(): row["Field"] for row in cur.fetchall()}
    return CENTRAL_USER_COLUMNS


def central_user_columns():
    columns = central_user_column_map()
    name_col = first_existing_column(
        columns,
        ["name", "full_name", "user_name", "username", "employee_name", "emp_name", "display_name"],
    )
    dob_col = first_existing_column(
        columns,
        ["dob", "date_of_birth", "birth_date", "d_o_b", "dateofbirth"],
    )
    id_col = first_existing_column(
        columns,
        ["id", "user_id", "employee_id", "emp_id"],
    )
    active_col = first_existing_column(
        columns,
        ["active", "is_active", "enabled", "status", "user_status"],
    )
    if not name_col or not dob_col:
        raise RuntimeError("central_user_database.user needs a name and DOB column for login.")
    return {
        "id": id_col,
        "name": name_col,
        "dob": dob_col,
        "active": active_col,
    }


def active_user_sql(active_col):
    if not active_col:
        return ""
    col = quote_identifier(active_col)
    return (
        f" AND (CAST(COALESCE({col}, '') AS CHAR) IN ('1', 'Y', 'y', 'A', 'a') "
        f"OR LOWER(CAST(COALESCE({col}, '') AS CHAR)) IN ('active', 'enabled', 'yes', 'true'))"
    )


def dob_password(value):
    """Return DOB as DDMMYYYY, matching the user's required password format."""
    if isinstance(value, datetime):
        return value.strftime("%d%m%Y")
    if isinstance(value, date):
        return value.strftime("%d%m%Y")
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y", "%d%m%Y", "%Y%m%d"):
        try:
            parsed = datetime.strptime(text[:10] if "-" in text or "/" in text or "." in text else text[:8], fmt)
            return parsed.strftime("%d%m%Y")
        except ValueError:
            pass
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        eight = digits[:8]
        if eight[:4].isdigit() and 1900 <= int(eight[:4]) <= 2100:
            return eight[6:8] + eight[4:6] + eight[:4]
        return eight
    return ""


def find_login_user(name):
    cols = central_user_columns()
    id_expr = quote_identifier(cols["id"]) if cols["id"] else "NULL"
    name_col = quote_identifier(cols["name"])
    dob_col = quote_identifier(cols["dob"])
    with central_user_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {id_expr} AS user_id,
                       {name_col} AS display_name,
                       {dob_col} AS dob_value
                FROM `user`
                WHERE LOWER(TRIM({name_col})) = LOWER(TRIM(%s))
                {active_user_sql(cols["active"])}
                LIMIT 1
                """,
                [name],
            )
            return cur.fetchone()


def active_user_suggestions(query):
    cols = central_user_columns()
    name_col = quote_identifier(cols["name"])
    with central_user_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT {name_col} AS display_name
                FROM `user`
                WHERE {name_col} IS NOT NULL
                  AND TRIM({name_col}) <> ''
                  AND {name_col} LIKE %s
                {active_user_sql(cols["active"])}
                ORDER BY {name_col}
                LIMIT 12
                """,
                [f"%{query}%"],
            )
            return [str(row["display_name"]).strip() for row in cur.fetchall()]


def safe_next_url():
    target = request.values.get("next")
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("dashboard")


PRINTER_SETTING_KEYS = {
    "report": "printer.report",
    "receipt": "printer.receipt",
    "label": "printer.label",
}


def list_windows_printers():
    """Return installed Windows printers without adding third-party packages."""
    script = """
Add-Type -AssemblyName System.Drawing
$defaultPrinter = (New-Object System.Drawing.Printing.PrinterSettings).PrinterName
$printers = @()
[System.Drawing.Printing.PrinterSettings]::InstalledPrinters | ForEach-Object {
    $printers += [PSCustomObject]@{ Name = $_; Default = ($_ -eq $defaultPrinter) }
}
$printers | ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
        if isinstance(payload, dict):
            payload = [payload]
        return [
            {"name": str(row.get("Name") or ""), "default": bool(row.get("Default"))}
            for row in payload
            if row.get("Name")
        ]
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        app.logger.exception("Printer list lookup failed")
        return []


def get_printer_settings():
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT setting_key, setting_value
                FROM app_settings
                WHERE setting_key IN (%s, %s, %s)
                """,
                list(PRINTER_SETTING_KEYS.values()),
            )
            rows = {row["setting_key"]: row["setting_value"] for row in cur.fetchall()}
    return {
        role: str(rows.get(setting_key) or "")
        for role, setting_key in PRINTER_SETTING_KEYS.items()
    }


def save_printer_setting(role, printer_name):
    setting_key = PRINTER_SETTING_KEYS[role]
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (setting_key, setting_value)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)
                """,
                [setting_key, printer_name or ""],
            )
        conn.commit()


def selected_printer(role):
    return get_printer_settings().get(role) or ""


def find_browser_executable():
    """Find a local Chromium browser for server-side label PDF rendering."""
    candidates = [
        os.getenv("LABEL_BROWSER_PATH"),
        shutil.which("msedge"),
        shutil.which("chrome"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def load_local_env():
    """Load local development settings without requiring an extra package."""
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_local_env()
ensure_local_schema()


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    user = session.get("auth_user")
    if user:
        g.current_user = user
        return None
    if request.method == "GET":
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    return redirect(url_for("login"))


@app.get("/login")
def login():
    if session.get("auth_user"):
        return redirect(safe_next_url())
    return render_template("login.html")


@app.post("/login")
def login_submit():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    if not username or not password:
        flash("Name aur DOB password dono required hain.", "error")
        return redirect(url_for("login", next=request.form.get("next", "")))
    try:
        user = find_login_user(username)
    except Exception as exc:
        app.logger.warning("Login lookup failed: %s", exc)
        flash("User database se connect nahi ho pa raha. DB access/permission check karo.", "error")
        return redirect(url_for("login", next=request.form.get("next", "")))
    if not user or dob_password(user.get("dob_value")) != re.sub(r"\D", "", password):
        flash("Invalid name ya DOB password.", "error")
        return redirect(url_for("login", next=request.form.get("next", "")))
    session.clear()
    session["auth_user"] = {
        "id": user.get("user_id"),
        "name": str(user.get("display_name") or username).strip(),
    }
    flash("Login successful.", "success")
    return redirect(safe_next_url())


@app.get("/login/users")
def login_user_suggestions():
    query = request.args.get("q", "").strip()
    if len(query) < 1:
        return jsonify([])
    try:
        return jsonify(active_user_suggestions(query))
    except Exception as exc:
        app.logger.warning("Login suggestions failed: %s", exc)
        return jsonify([])


@app.route("/printer-settings", methods=["GET", "POST"])
def printer_settings():
    printers = list_windows_printers()
    printer_names = {printer["name"] for printer in printers}
    if request.method == "POST":
        for role in PRINTER_SETTING_KEYS:
            value = str(request.form.get(role) or "").strip()
            if value and printer_names and value not in printer_names:
                flash(f"{value} is not currently connected.", "error")
                return redirect(url_for("printer_settings"))
            save_printer_setting(role, value)
        flash("Printer settings saved.", "success")
        return redirect(url_for("printer_settings"))
    return render_template(
        "printer_settings.html",
        printers=printers,
        settings=get_printer_settings(),
    )


@app.get("/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("login"))

SYNC_INTERVAL_SECONDS = int(os.getenv("REMOTE_SYNC_INTERVAL_SECONDS", "1200"))
SYNC_LOOKBACK_DAYS = int(os.getenv("REMOTE_SYNC_LOOKBACK_DAYS", "5"))
LABMATE_WEBHOOK_SYNC_INTERVAL_SECONDS = int(os.getenv("LABMATE_WEBHOOK_SYNC_INTERVAL_SECONDS", "3"))
PARTIAL_REVIEW_CHECK_INTERVAL_SECONDS = int(os.getenv("PARTIAL_REVIEW_CHECK_INTERVAL_SECONDS", "3"))
RECEIPT_CACHE_DIR = Path(os.getenv("RECEIPT_CACHE_DIR", Path(__file__).with_name("receipt_cache")))
RECEIPT_CACHE_QUEUE_LIMIT = int(os.getenv("RECEIPT_CACHE_QUEUE_LIMIT", "200"))
RECEIPT_CACHE_ENQUEUE_LIMIT = int(os.getenv("RECEIPT_CACHE_ENQUEUE_LIMIT", "40"))
RECEIPT_CACHE_WORKER_DELAY_SECONDS = float(os.getenv("RECEIPT_CACHE_WORKER_DELAY_SECONDS", "2.0"))

receipt_cache_queue = deque()
receipt_cache_pending = set()
receipt_cache_lock = threading.Lock()
receipt_cache_wakeup = threading.Event()


def sync_recent_remote_data():
    """Refresh the local patients/patient_tests mirror from the project process."""
    for days_ago in range(SYNC_LOOKBACK_DAYS - 1, -1, -1):
        booking_date = (date.today() - timedelta(days=days_ago)).isoformat()
        result = sync_booking_date(booking_date)
        app.logger.info("Remote sync complete for %s: %s", booking_date, result)
    enqueue_recent_receipt_cache_candidates()


def start_remote_sync_worker():
    """Start one daemon worker: immediately sync, then repeat every 20 minutes."""
    def worker():
        while True:
            try:
                sync_recent_remote_data()
            except Exception:
                app.logger.exception("Remote patient sync failed; will retry in %s seconds", SYNC_INTERVAL_SECONDS)
            threading.Event().wait(SYNC_INTERVAL_SECONDS)

    threading.Thread(target=worker, name="remote-patient-sync", daemon=True).start()


def start_receipt_cache_worker():
    """Slow background worker for receipt prefetching; never blocks dashboard or sync."""
    def worker():
        while True:
            receipt_cache_wakeup.wait(timeout=60)
            receipt_cache_wakeup.clear()
            while True:
                with receipt_cache_lock:
                    if not receipt_cache_queue:
                        break
                    patient_id = receipt_cache_queue.popleft()
                    receipt_cache_pending.discard(patient_id)
                try:
                    cached_receipt_pdf(patient_id)
                except Exception as exc:
                    app.logger.warning("Receipt cache failed for patient_id=%s: %s", patient_id, exc)
                time.sleep(RECEIPT_CACHE_WORKER_DELAY_SECONDS)

    threading.Thread(target=worker, name="receipt-cache-worker", daemon=True).start()


def check_waiting_partial_reviews():
    """Move waiting partial reviews back to unseen when LabMate reports new completed tests."""
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT patient_id, partial_review_webhook_id, partial_review_completed_hash
                FROM patients
                WHERE partial_review_status='wait'
                ORDER BY partial_review_at ASC
                LIMIT 500
                """
            )
            waiting_patients = cur.fetchall()

    if not waiting_patients:
        return 0

    patient_ids = [patient["patient_id"] for patient in waiting_patients]
    placeholders = ", ".join(["%s"] * len(patient_ids))
    with pymysql.connect(
        host=os.getenv("LABMATE_DB_HOST", "10.1.1.44"),
        port=3306,
        user=os.getenv("LABMATE_DB_USER", "sahil"),
        password=os.environ["LABMATE_DB_PASSWORD"],
        database="lead_management",
        cursorclass=pymysql.cursors.DictCursor,
    ) as source:
        with source.cursor() as cur:
            cur.execute(
                f"""
                SELECT webhook.id, webhook.patient_id, webhook.test_list
                FROM labmate_webhook_logs AS webhook
                INNER JOIN (
                    SELECT patient_id, MAX(id) AS latest_id
                    FROM labmate_webhook_logs
                    WHERE patient_id IN ({placeholders})
                    GROUP BY patient_id
                ) AS latest ON latest.latest_id = webhook.id
                """,
                patient_ids,
            )
            latest_by_patient = {
                int(row["patient_id"]): {
                    "id": row["id"],
                    "completed_hash": completed_tests_hash(completed_test_codes(row["test_list"])),
                }
                for row in cur.fetchall()
                if str(row["patient_id"]).isdigit()
            }

    changed = []
    for patient in waiting_patients:
        latest = latest_by_patient.get(patient["patient_id"])
        if not latest:
            continue
        saved_hash = str(patient.get("partial_review_completed_hash") or "")
        saved_webhook_id = patient.get("partial_review_webhook_id")
        if saved_webhook_id != latest["id"] and saved_hash != latest["completed_hash"]:
            changed.append((latest["id"], latest["completed_hash"], patient["patient_id"]))

    if not changed:
        return 0

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                UPDATE patients
                SET partial_review_status='unseen',
                    partial_review_webhook_id=%s,
                    partial_review_completed_hash=%s,
                    partial_review_at=NOW()
                WHERE patient_id=%s AND partial_review_status='wait'
                """,
                changed,
            )
        conn.commit()
    return len(changed)


def start_partial_review_checker_worker():
    """Continuously re-open waiting partial reviews when a newer webhook completes more tests."""
    def worker():
        while True:
            try:
                changed = check_waiting_partial_reviews()
                if changed:
                    app.logger.info("Partial review checker reset %s waiting patient(s) to unseen.", changed)
            except Exception:
                app.logger.exception(
                    "Partial review checker failed; will retry in %s seconds",
                    PARTIAL_REVIEW_CHECK_INTERVAL_SECONDS,
                )
            threading.Event().wait(PARTIAL_REVIEW_CHECK_INTERVAL_SECONDS)

    threading.Thread(target=worker, name="partial-review-checker", daemon=True).start()


def db_connection():
    return local_connection()


def record_print_attempt(patient_id, status, stage, receipt_included=False, error_message=None, batch_id=None, booking_label=None):
    """Persist one local print outcome for audit and troubleshooting."""
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO print_attempts
                    (batch_id, patient_id, booking_label, status, stage, receipt_included, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [batch_id, patient_id, booking_label, status, stage, bool(receipt_included), error_message],
            )
        conn.commit()


def delivery_label(mode):
    codes = [code.strip() for code in (mode or "").split(",") if code.strip()]
    names = [DELIVERY_LABELS.get(code, code) for code in codes if code in DELIVERY_LABELS]
    return ", ".join(names) or "Not set"


def has_labmate_courier_delivery(mode):
    return bool({code.strip() for code in (mode or "").split(",")} & {"C", "H"})


def home_delivery_label(value):
    modes = {item.strip().lower() for item in (value or "").split(",")}
    labels = []
    if "courier" in modes:
        labels.append("Courier")
    if "by_hand" in modes:
        labels.append("By Hand")
    return ", ".join(labels)


def delivery_marker_from_label(label):
    """Return compact label markers: Courier=C, By Hand=H."""
    text = str(label or "").lower()
    markers = []
    if "courier" in text:
        markers.append("C")
    if "by hand" in text or "by_hand" in text:
        markers.append("H")
    return "/".join(markers)


def booking_id_from_slip(req_slip_no):
    match = re.search(r"(\d+)$", str(req_slip_no or ""))
    return int(match.group(1)) if match else None


def charge_mode_label(charge_as):
    return CHARGE_MODE_LABELS.get(str(charge_as or ""), "Not set")


def can_access_report(charge_as, balance):
    """Free and Credit reports are eligible; Paying reports require no pending due."""
    mode = str(charge_as or "")
    return mode in {"2", "3"} or (mode == "1" and Decimal(str(balance or 0)) <= 0)


def completed_test_codes(test_list):
    """Extract test codes from the latest webhook's JSON test list."""
    if not test_list:
        return set()
    try:
        items = json.loads(test_list)
    except (TypeError, json.JSONDecodeError):
        return set()
    if not isinstance(items, list):
        return set()
    codes = set()
    for item in items:
        # Webhook values are currently stored as "TEST_CODE - Test name".
        code = str(item).split(" - ", 1)[0].strip().upper()
        if code:
            codes.add(code)
    return codes


def completed_tests_hash(completed_codes):
    return hashlib.sha1("|".join(sorted(completed_codes)).encode("utf-8")).hexdigest()


def completion_state(report_status, completed_count):
    """Map the latest LabMate webhook status to the dispatch completion filter."""
    status = str(report_status or "").strip().casefold()
    compact_status = " ".join(status.split())
    if "partial" in compact_status or compact_status.startswith("part "):
        return "partial"
    if compact_status in {"authenticated", "authenticate"}:
        return "ready"
    # Booked/empty/other statuses with no completed item belong in Not complete.
    return "not-complete" if not completed_count else "partial"


def latest_webhook_for_patient(patient_id):
    with pymysql.connect(
        host=os.getenv("LABMATE_DB_HOST", "10.1.1.44"),
        port=3306,
        user=os.getenv("LABMATE_DB_USER", "sahil"),
        password=os.environ["LABMATE_DB_PASSWORD"],
        database="lead_management",
        cursorclass=pymysql.cursors.DictCursor,
    ) as source:
        with source.cursor() as cur:
            cur.execute(
                """
                SELECT id, test_list, r_without_lh, reportStatus
                FROM labmate_webhook_logs
                WHERE patient_id=%s
                ORDER BY id DESC
                LIMIT 1
                """,
                [patient_id],
            )
            row = cur.fetchone() or {}
    completed_codes = completed_test_codes(row.get("test_list"))
    return {
        "id": row.get("id"),
        "report_path": str(row.get("r_without_lh") or "").strip(),
        "report_status": str(row.get("reportStatus") or "").strip(),
        "completed_codes": completed_codes,
        "completed_hash": completed_tests_hash(completed_codes),
    }


def fetch_latest_webhooks(patient_ids):
    """Return latest LabMate webhook rows keyed by patient_id."""
    if not patient_ids:
        return {}
    latest = {}
    with pymysql.connect(
        host=os.getenv("LABMATE_DB_HOST", "10.1.1.44"),
        port=3306,
        user=os.getenv("LABMATE_DB_USER", "sahil"),
        password=os.environ["LABMATE_DB_PASSWORD"],
        database="lead_management",
        cursorclass=pymysql.cursors.DictCursor,
    ) as source:
        with source.cursor() as cur:
            for index in range(0, len(patient_ids), 500):
                patient_chunk = patient_ids[index : index + 500]
                placeholders = ", ".join(["%s"] * len(patient_chunk))
                cur.execute(
                    f"""
                    SELECT webhook.id, webhook.patient_id, webhook.test_list,
                           webhook.r_without_lh, webhook.reportStatus
                    FROM labmate_webhook_logs AS webhook
                    INNER JOIN (
                        SELECT patient_id, MAX(id) AS latest_id
                        FROM labmate_webhook_logs
                        WHERE patient_id IN ({placeholders})
                        GROUP BY patient_id
                    ) AS latest ON latest.latest_id = webhook.id
                    """,
                    patient_chunk,
                )
                for row in cur.fetchall():
                    if not str(row["patient_id"]).isdigit():
                        continue
                    completed_codes = completed_test_codes(row.get("test_list"))
                    latest[int(row["patient_id"])] = {
                        "id": row.get("id"),
                        "test_list": row.get("test_list"),
                        "report_path": str(row.get("r_without_lh") or "").strip(),
                        "report_status": str(row.get("reportStatus") or "").strip(),
                        "completed_codes": completed_codes,
                        "completed_hash": completed_tests_hash(completed_codes),
                    }
    return latest


def sync_current_date_webhooks():
    """Cache latest LabMate webhook data for every current-date local patient."""
    today = date.today().isoformat()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT patient_id, webhook_id, webhook_report_path,
                       webhook_report_status, webhook_completed_hash
                FROM patients
                WHERE booking_date=%s
                ORDER BY patient_id
                """,
                [today],
            )
            current_rows = cur.fetchall()
            patient_ids = [row["patient_id"] for row in current_rows]
            current_by_patient = {row["patient_id"]: row for row in current_rows}

    if not patient_ids:
        return {"checked": 0, "updated": 0, "reset_waiting": 0}

    latest_by_patient = fetch_latest_webhooks(patient_ids)
    if not latest_by_patient:
        return {"checked": len(patient_ids), "updated": 0, "reset_waiting": 0}

    latest_patient_ids = list(latest_by_patient.keys())
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT patient_id, partial_review_webhook_id, partial_review_completed_hash
                FROM patients
                WHERE booking_date=%s
                  AND partial_review_status='wait'
                  AND patient_id IN ({", ".join(["%s"] * len(latest_patient_ids))})
                """,
                [today, *latest_patient_ids],
            )
            waiting_rows = cur.fetchall()

    reset_waiting = []
    for row in waiting_rows:
        webhook = latest_by_patient.get(row["patient_id"])
        if not webhook:
            continue
        if (
            row.get("partial_review_webhook_id") != webhook["id"]
            and str(row.get("partial_review_completed_hash") or "") != webhook["completed_hash"]
        ):
            reset_waiting.append((webhook["id"], webhook["completed_hash"], row["patient_id"]))

    updates = []
    for patient_id, webhook in latest_by_patient.items():
        current = current_by_patient.get(patient_id) or {}
        if (
            current.get("webhook_id") == webhook["id"]
            and str(current.get("webhook_completed_hash") or "") == webhook["completed_hash"]
            and str(current.get("webhook_report_path") or "") == webhook["report_path"]
            and str(current.get("webhook_report_status") or "") == webhook["report_status"]
        ):
            continue
        updates.append(
            (
                webhook["id"],
                webhook["test_list"],
                webhook["report_path"],
                webhook["report_status"],
                webhook["completed_hash"],
                patient_id,
            )
        )
    with db_connection() as conn:
        with conn.cursor() as cur:
            if updates:
                cur.executemany(
                    """
                    UPDATE patients
                    SET webhook_id=%s,
                        webhook_test_list=%s,
                        webhook_report_path=%s,
                        webhook_report_status=%s,
                        webhook_completed_hash=%s,
                        webhook_synced_at=NOW()
                    WHERE patient_id=%s
                    """,
                    updates,
                )
                updated = cur.rowcount
            else:
                updated = 0
            if reset_waiting:
                cur.executemany(
                    """
                    UPDATE patients
                    SET partial_review_status='unseen',
                        partial_review_webhook_id=%s,
                        partial_review_completed_hash=%s,
                        partial_review_at=NOW()
                    WHERE patient_id=%s AND partial_review_status='wait'
                    """,
                    reset_waiting,
                )
        conn.commit()
    return {"checked": len(patient_ids), "updated": updated, "reset_waiting": len(reset_waiting)}


def start_current_date_webhook_sync_worker():
    """Poll LabMate webhook logs for all current-date patients every few seconds."""
    def worker():
        while True:
            try:
                result = sync_current_date_webhooks()
                if result["updated"] or result["reset_waiting"]:
                    app.logger.info(
                        "LabMate webhook sync checked %s current-date patient(s), updated %s row(s), reset %s to unseen.",
                        result["checked"],
                        result["updated"],
                        result["reset_waiting"],
                    )
            except Exception:
                app.logger.exception(
                    "LabMate webhook sync failed; will retry in %s seconds",
                    LABMATE_WEBHOOK_SYNC_INTERVAL_SECONDS,
                )
            threading.Event().wait(LABMATE_WEBHOOK_SYNC_INTERVAL_SECONDS)

    threading.Thread(target=worker, name="current-date-webhook-sync", daemon=True).start()


def has_pending_tests(patient_id):
    """True when an active booked test is absent from the patient's latest webhook list."""
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT test_code FROM patient_tests WHERE patient_id = %s AND active = 1",
                [patient_id],
            )
            booked_codes = {str(row["test_code"]).strip().upper() for row in cur.fetchall()}
            if not booked_codes:
                # A patient with no active booked test must not be printable.
                return True
    with pymysql.connect(host=os.getenv("LABMATE_DB_HOST", "10.1.1.44"),port=3306,user=os.getenv("LABMATE_DB_USER", "sahil"),password=os.environ["LABMATE_DB_PASSWORD"],database="lead_management",cursorclass=pymysql.cursors.DictCursor) as source:
        with source.cursor() as cur:
            cur.execute(
                """
                SELECT test_list FROM labmate_webhook_logs
                WHERE patient_id = %s ORDER BY id DESC LIMIT 1
                """,
                [patient_id],
            )
            latest_webhook = cur.fetchone()
    completed_codes = completed_test_codes(latest_webhook["test_list"] if latest_webhook else None)
    return any(not is_catalog_test_completed(code, completed_codes) for code in booked_codes)


def is_catalog_test_completed(booked_code, completed_codes, visiting=()):
    """A profile is complete when every direct child completes recursively."""
    catalog_test = CATALOG_PRELOAD.get_test_by_booked_code(booked_code)
    if not catalog_test or not catalog_test.is_profile:
        return str(booked_code).strip().upper() in completed_codes
    if catalog_test.key_string in visiting:
        return False
    children = CATALOG_PRELOAD.profile_children.get(catalog_test.key_string, [])
    if not children:
        return str(booked_code).strip().upper() in completed_codes
    next_visiting = (*visiting, catalog_test.key_string)
    return all(
        is_catalog_test_completed(child.booked_code, completed_codes, next_visiting)
        for child in children
    )


def catalog_child_tree(catalog_test, completed_codes, ancestors=()):
    """Build a direct-child tree for the UI while preventing profile cycles."""
    if not catalog_test or catalog_test.key_string in ancestors:
        return []
    next_ancestors = (*ancestors, catalog_test.key_string)
    children = []
    for child in CATALOG_PRELOAD.profile_children.get(catalog_test.key_string, []):
        children.append(
            {
                "code": child.booked_code,
                "name": child.name,
                "is_profile": child.is_profile,
                "is_completed": is_catalog_test_completed(child.booked_code, completed_codes),
                "children": catalog_child_tree(child, completed_codes, next_ancestors)
                if child.is_profile
                else [],
            }
        )
    return children


def scalar(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y, %I:%M %p")
    return value


def report_url():
    return "http://10.1.1.178:8000/downloadnew/PatientReportDirectView.aspx"


def clean_join(parts, separator=", "):
    return separator.join(str(part).strip() for part in parts if str(part or "").strip())


def label_report_id(patient):
    """Return patient ID text for courier label; mark paying patients with cached receipts."""
    patient_id = str(patient.get("patient_id") or "").strip()
    is_paying = str(patient.get("charge_as") or "").strip() == "1"
    has_receipt = bool(str(patient.get("receipt_cached_path") or "").strip())
    return f"{patient_id}*" if patient_id and is_paying and has_receipt else patient_id


def excel_column_name(index):
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def xlsx_inline_cell(value, row_index, column_index, style_id=None):
    cell_ref = f"{excel_column_name(column_index)}{row_index}"
    style_attr = f' s="{style_id}"' if style_id is not None else ""
    text = "" if value is None else str(value)
    return (
        f'<c r="{cell_ref}" t="inlineStr"{style_attr}>'
        f"<is><t>{xml_escape(text)}</t></is></c>"
    )


def build_simple_xlsx(headers, rows, column_widths=None, sheet_name="Sheet1"):
    """Create a lightweight .xlsx without adding runtime dependencies."""
    column_widths = column_widths or {}
    sheet_rows = []
    header_cells = [
        xlsx_inline_cell(header, 1, index, style_id=1)
        for index, header in enumerate(headers, start=1)
    ]
    sheet_rows.append(f'<row r="1">{"".join(header_cells)}</row>')
    for row_index, row in enumerate(rows, start=2):
        cells = [
            xlsx_inline_cell(value, row_index, column_index)
            for column_index, value in enumerate(row, start=1)
        ]
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    columns_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in column_widths.items()
    )
    max_column = excel_column_name(len(headers))
    dimension = f"A1:{max_column}{max(len(rows) + 1, 1)}"
    worksheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<dimension ref="{dimension}"/>
<cols>{columns_xml}</cols>
<sheetData>{"".join(sheet_rows)}</sheetData>
</worksheet>'''
    workbook_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{xml_escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>
</workbook>'''
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
</styleSheet>'''
    content_types_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>'''
    root_rels_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''
    workbook_rels_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", root_rels_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet_xml)
        archive.writestr("xl/styles.xml", styles_xml)
    output.seek(0)
    return output.getvalue()


def snapshot_value(snapshot, *keys):
    """Return the first non-empty value from a possibly nested address snapshot."""
    if not isinstance(snapshot, dict):
        return ""
    wanted = {str(key).lower() for key in keys}
    stack = [snapshot]
    while stack:
        item = stack.pop(0)
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if str(key).lower() in wanted and str(value or "").strip():
                return value
            if isinstance(value, dict):
                stack.append(value)
    return ""


def clean_address_value(value):
    value = str(value or "").strip()
    value = re.sub(r"[_]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" ,-")


def clean_floor_value(value):
    value = clean_address_value(value)
    if not value:
        return ""
    normalized = value.lower().replace(".", "")
    floor_map = {
        "g": "Ground Floor",
        "gf": "Ground Floor",
        "ground": "Ground Floor",
        "ground f": "Ground Floor",
        "ground floor": "Ground Floor",
        "basement": "Basement",
        "b": "Basement",
        "lg": "Lower Ground Floor",
        "lower ground": "Lower Ground Floor",
        "lower ground floor": "Lower Ground Floor",
        "first": "1st Floor",
        "1": "1st Floor",
        "1st": "1st Floor",
        "1st floor": "1st Floor",
        "second": "2nd Floor",
        "2": "2nd Floor",
        "2nd": "2nd Floor",
        "2nd floor": "2nd Floor",
        "third": "3rd Floor",
        "3": "3rd Floor",
        "3rd": "3rd Floor",
        "3rd floor": "3rd Floor",
    }
    if normalized in floor_map:
        return floor_map[normalized]
    if "floor" not in normalized:
        return f"Floor: {value}"
    return value


def address_from_snapshot(snapshot):
    if not snapshot:
        return ""
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except json.JSONDecodeError:
            return snapshot.strip()
    if not isinstance(snapshot, dict):
        return ""

    house = clean_address_value(
        snapshot_value(snapshot, "house_flat_no", "house_no", "flat_no", "flat", "house", "address1")
    )
    floor = clean_floor_value(snapshot_value(snapshot, "floor_display", "floor", "floor_no", "floor_number"))
    block = clean_address_value(
        snapshot_value(snapshot, "block_tower_no", "block", "tower", "tower_no", "building", "building_name")
    )
    street = clean_address_value(
        snapshot_value(snapshot, "street_line", "street_sector", "street", "sector", "lane", "address2")
    )
    landmark = clean_address_value(snapshot_value(snapshot, "landmark", "near_by", "nearby", "near"))
    colony = clean_address_value(snapshot_value(snapshot, "colony_name", "colony", "area", "locality", "location"))
    city = clean_address_value(snapshot_value(snapshot, "city", "city_name"))
    pincode = clean_address_value(snapshot_value(snapshot, "pincode", "pin_code", "postal_code", "zip"))

    line1 = clean_join([house, floor, block], ", ")
    line2 = clean_join([street, landmark], ", ")
    line3 = clean_join([colony], ", ")
    line4 = clean_join([city, pincode], " - ")
    formatted = clean_join([line1, line2, line3, line4], "\n")
    if formatted:
        return formatted
    return clean_address_value(snapshot_value(snapshot, "address", "full_address", "address_line"))

def lead_management_connection():
    return pymysql.connect(
        host=os.getenv("LEAD_MANAGEMENT_DB_HOST", "10.1.1.67"),
        port=int(os.getenv("LEAD_MANAGEMENT_DB_PORT", "3306")),
        user=os.getenv("LEAD_MANAGEMENT_DB_USER", "sahil"),
        password=os.getenv("LEAD_MANAGEMENT_DB_PASSWORD", "sahil@123"),
        database=os.getenv("LEAD_MANAGEMENT_DB_NAME", "lead_management"),
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_courier_address(patient_ids, booking_label):
    """Read courier label address from hhome_collection_booking.address_snapshot_json."""
    try:
        with lead_management_connection() as conn:
            with conn.cursor() as cur:
                booking_number = booking_id_from_slip(booking_label)
                if not booking_label and not booking_number:
                    return ""
                cur.execute(
                    """
                    SELECT address_snapshot_json
                    FROM hhome_collection_booking
                    WHERE booking_code=%s OR id=%s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    [booking_label, booking_number],
                )
                row = cur.fetchone()
                if row:
                    return address_from_snapshot(row.get("address_snapshot_json"))
    except Exception as exc:
        app.logger.warning("Courier label address lookup failed for %s: %s", booking_label, exc)
    return ""


def fetch_home_report_delivery_label(booking_label):
    """Read report delivery mode from home collection booking when LabMate mode is absent."""
    try:
        with lead_management_connection() as conn:
            with conn.cursor() as cur:
                booking_number = booking_id_from_slip(booking_label)
                if not booking_label and not booking_number:
                    return ""
                cur.execute(
                    """
                    SELECT report_delivery
                    FROM hhome_collection_booking
                    WHERE booking_code=%s OR id=%s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    [booking_label, booking_number],
                )
                row = cur.fetchone()
                if row:
                    return home_delivery_label(row.get("report_delivery"))
    except Exception as exc:
        app.logger.warning("Courier label delivery lookup failed for %s: %s", booking_label, exc)
    return ""


def code128_b_svg(value, width=360, height=48):
    """Render a Code 128-B barcode as inline SVG for ASCII label IDs."""
    patterns = [
        "212222","222122","222221","121223","121322","131222","122213","122312","132212","221213",
        "221312","231212","112232","122132","122231","113222","123122","123221","223211","221132",
        "221231","213212","223112","312131","311222","321122","321221","312212","322112","322211",
        "212123","212321","232121","111323","131123","131321","112313","132113","132311","211313",
        "231113","231311","112133","112331","132131","113123","113321","133121","313121","211331",
        "231131","213113","213311","213131","311123","311321","331121","312113","312311","332111",
        "314111","221411","431111","111224","111422","121124","121421","141122","141221","112214",
        "112412","122114","122411","142112","142211","241211","221114","413111","241112","134111",
        "111242","121142","121241","114212","124112","124211","411212","421112","421211","212141",
        "214121","412121","111143","111341","131141","114113","114311","411113","411311","113141",
        "114131","311141","411131","211412","211214","211232","2331112",
    ]
    chars = [ord(char) - 32 for char in str(value) if 32 <= ord(char) <= 127]
    checksum = 104
    for index, code in enumerate(chars, start=1):
        checksum += code * index
    codes = [104, *chars, checksum % 103, 106]
    module_widths = "".join(patterns[code] for code in codes)
    total_modules = sum(int(width_char) for width_char in module_widths)
    scale = width / total_modules
    x = 0
    rects = []
    is_bar = True
    for width_char in module_widths:
        bar_width = int(width_char) * scale
        if is_bar:
            rects.append(
                f'<rect x="{x:.2f}" y="0" width="{bar_width:.2f}" height="{height}" fill="#000"/>'
            )
        x += bar_width
        is_bar = not is_bar
    return f'<svg class="barcode" viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-label="{value}">{"".join(rects)}</svg>'


def next_envelope_id(booking_date):
    prefix = f"ENV-{booking_date.strftime('%y%m%d')}-"
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT envelope_id
                FROM (
                    SELECT envelope_id FROM courier_envelopes WHERE envelope_id LIKE %s
                    UNION ALL
                    SELECT envelope_id FROM voided_courier_envelopes WHERE envelope_id LIKE %s
                ) AS all_envelopes
                ORDER BY envelope_id DESC
                LIMIT 1
                """,
                [prefix + "%", prefix + "%"],
            )
            row = cur.fetchone()
    next_number = 1
    if row:
        match = re.search(r"-(\d+)$", row["envelope_id"])
        if match:
            next_number = int(match.group(1)) + 1
    return f"{prefix}{next_number:04d}"


def next_envelope_id_with_cursor(cur, booking_date):
    prefix = f"ENV-{booking_date.strftime('%y%m%d')}-"
    cur.execute(
        """
        SELECT envelope_id
        FROM (
            SELECT envelope_id FROM courier_envelopes WHERE envelope_id LIKE %s
            UNION ALL
            SELECT envelope_id FROM voided_courier_envelopes WHERE envelope_id LIKE %s
        ) AS all_envelopes
        ORDER BY envelope_id DESC
        LIMIT 1
        """,
        [prefix + "%", prefix + "%"],
    )
    row = cur.fetchone()
    next_number = 1
    if row:
        match = re.search(r"-(\d+)$", row["envelope_id"])
        if match:
            next_number = int(match.group(1)) + 1
    return f"{prefix}{next_number:04d}"


def envelope_source_signature(patient_ids_text, recipient_name, address, mobile_no, delivery_marker):
    payload = {
        "patient_ids": patient_ids_text,
        "recipient_name": str(recipient_name or "").strip(),
        "recipient_address": str(address or "").strip(),
        "mobile_no": str(mobile_no or "").strip(),
        "delivery_marker": str(delivery_marker or "").strip(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def void_envelope_id(cur, envelope, replacement_id, reason):
    old_id = str(envelope.get("envelope_id") or "").strip()
    if not old_id:
        return
    cur.execute(
        """
        INSERT IGNORE INTO voided_courier_envelopes
            (envelope_id, booking_key, booking_label, replaced_by_envelope_id, reason)
        VALUES (%s, %s, %s, %s, %s)
        """,
        [
            old_id,
            envelope.get("booking_key"),
            envelope.get("booking_label"),
            replacement_id,
            reason,
        ],
    )


def get_or_create_courier_envelope(patients):
    booking_label = str(patients[0].get("req_slip_no") or "").strip() or "Unknown group"
    booking_date = patients[0].get("booking_date") or date.today()
    if isinstance(booking_date, str):
        booking_date = datetime.strptime(booking_date, "%Y-%m-%d").date()
    patient_ids = [int(patient["patient_id"]) for patient in patients]
    booking_key = booking_label if booking_label != "Unknown group" else "patient-" + "-".join(map(str, patient_ids))
    patient_ids_text = ",".join(map(str, patient_ids))
    names = [str(patient.get("patient_name") or "").strip() for patient in patients]
    recipient_name = clean_join(names, ", ") or "Unnamed patient"
    mobile_no = patients[0].get("mobile_no") or ""
    address = fetch_courier_address(patient_ids, booking_label)
    delivery_labels = [
        delivery_label(patient.get("delivery_mode"))
        for patient in patients
        if has_labmate_courier_delivery(patient.get("delivery_mode"))
    ]
    if not delivery_labels:
        delivery_labels = [fetch_home_report_delivery_label(booking_label)]
    delivery_marker = delivery_marker_from_label(", ".join(delivery_labels))
    source_signature = envelope_source_signature(
        patient_ids_text,
        recipient_name,
        address,
        mobile_no,
        delivery_marker,
    )

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM courier_envelopes WHERE booking_key=%s LIMIT 1", [booking_key])
            envelope = cur.fetchone()
            if not envelope:
                envelope_id = next_envelope_id_with_cursor(cur, booking_date)
                cur.execute(
                    """
                    INSERT INTO courier_envelopes
                        (envelope_id, booking_key, booking_label, booking_date, recipient_name,
                         recipient_address, mobile_no, patient_ids, source_signature)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        envelope_id,
                        booking_key,
                        booking_label,
                        booking_date,
                        recipient_name,
                        address,
                        mobile_no,
                        patient_ids_text,
                        source_signature,
                    ],
                )
                cur.execute("SELECT * FROM courier_envelopes WHERE id=LAST_INSERT_ID()")
                envelope = cur.fetchone()
            else:
                previous_signature = str(envelope.get("source_signature") or "")
                field_changed = any(
                    [
                        str(envelope.get("recipient_name") or "").strip() != str(recipient_name or "").strip(),
                        str(envelope.get("recipient_address") or "").strip() != str(address or "").strip(),
                        str(envelope.get("mobile_no") or "").strip() != str(mobile_no or "").strip(),
                        str(envelope.get("patient_ids") or "").strip() != str(patient_ids_text or "").strip(),
                    ]
                )
                data_changed = (bool(previous_signature) and previous_signature != source_signature) or (
                    not previous_signature and field_changed
                )
                envelope_id = envelope["envelope_id"]
                if data_changed:
                    envelope_id = next_envelope_id_with_cursor(cur, booking_date)
                    void_envelope_id(cur, envelope, envelope_id, "Label source data changed")
                data_changed_flag = 1 if data_changed else 0
                cur.execute(
                    """
                    UPDATE courier_envelopes
                    SET envelope_id=%s,
                        booking_label=%s,
                        booking_date=%s,
                        recipient_name=%s,
                        recipient_address=%s,
                        mobile_no=%s,
                        patient_ids=%s,
                        source_signature=%s,
                        status=CASE WHEN %s THEN 'not_printed' ELSE status END,
                        printed_at=CASE WHEN %s THEN NULL ELSE printed_at END,
                        printed_by=CASE WHEN %s THEN NULL ELSE printed_by END,
                        reprint_count=CASE WHEN %s THEN 0 ELSE reprint_count END,
                        latest_reprint_at=CASE WHEN %s THEN NULL ELSE latest_reprint_at END
                    WHERE id=%s
                    """,
                    [
                        envelope_id,
                        booking_label,
                        booking_date,
                        recipient_name,
                        address,
                        mobile_no,
                        patient_ids_text,
                        source_signature,
                        data_changed_flag,
                        data_changed_flag,
                        data_changed_flag,
                        data_changed_flag,
                        data_changed_flag,
                        envelope["id"],
                    ],
                )
                conn.commit()
                cur.execute("SELECT * FROM courier_envelopes WHERE id=%s", [envelope["id"]])
                envelope = cur.fetchone()
        conn.commit()
    envelope["patients"] = patients
    envelope["report_ids"] = [label_report_id(patient) for patient in patients]
    envelope["barcode_svg"] = code128_b_svg(envelope["envelope_id"])
    envelope["address_missing"] = not bool(str(envelope.get("recipient_address") or "").strip())
    envelope["delivery_marker"] = delivery_marker or "?"
    return envelope


def print_pdf(pdf_bytes, printer_name=None):
    """Render every PDF consistently to the default printer's usable page area."""
    with tempfile.NamedTemporaryFile(prefix="arpra-report-", suffix=".pdf", delete=False) as file:
        file.write(pdf_bytes)
        pdf_path = file.name

    # Do not delegate to Chrome/another associated PDF app: those apps can
    # apply their own shrink-to-fit settings. The renderer below uses the
    # printer's actual page bounds for both receipt and report PDFs.
    try:
        print_rendered_pdf(pdf_path, printer_name=printer_name)
    finally:
        Path(pdf_path).unlink(missing_ok=True)


def print_rendered_pdf(pdf_path, printer_name=None):
    """Render a PDF into page images and print them through the default printer."""
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        # The bundled Poppler runtime is not always inherited by a Flask
        # process started from Explorer/PowerShell, so also find it by path.
        bundled_tools = sorted(
            (Path.home() / ".cache" / "codex-runtimes").glob(
                "*/dependencies/native/poppler/Library/bin/pdftoppm.exe"
            )
        )
        if bundled_tools:
            pdftoppm = str(bundled_tools[-1])
    if not pdftoppm:
        raise RuntimeError("No PDF print handler is installed and pdftoppm is unavailable.")

    render_dir = Path(tempfile.mkdtemp(prefix="arpra-render-"))
    try:
        page_prefix = render_dir / "page"
        rendered = subprocess.run(
            [pdftoppm, "-png", "-r", "220", pdf_path, str(page_prefix)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        pages = sorted(render_dir.glob("page-*.png"))
        if rendered.returncode != 0 or not pages:
            raise RuntimeError("Could not render the PDF for printing: " + rendered.stderr.strip())

        script_path = render_dir / "print-images.ps1"
        script_path.write_text(
            """param([string]$PrinterName, [Parameter(ValueFromRemainingArguments=$true)][string[]]$ImagePaths)
Add-Type -AssemblyName System.Drawing
$script:pageIndex = 0
$document = New-Object System.Drawing.Printing.PrintDocument
if ($PrinterName) { $document.PrinterSettings.PrinterName = $PrinterName }
$document.OriginAtMargins = $false
$document.DefaultPageSettings.Margins = New-Object System.Drawing.Printing.Margins(0, 0, 0, 0)
$document.add_PrintPage({
    param($sender, $eventArgs)
    $image = [System.Drawing.Image]::FromFile($ImagePaths[$script:pageIndex])
    try {
        # PageBounds is the full printable sheet, not the application's
        # default one-inch MarginBounds area. Keep the PDF aspect ratio while
        # filling as much of the physical page as the printer permits.
        $bounds = $eventArgs.PageBounds
        $scale = [Math]::Min($bounds.Width / $image.Width, $bounds.Height / $image.Height)
        $width = [int]($image.Width * $scale)
        $height = [int]($image.Height * $scale)
        $x = $bounds.X + [int](($bounds.Width - $width) / 2)
        $y = $bounds.Y + [int](($bounds.Height - $height) / 2)
        $eventArgs.Graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $eventArgs.Graphics.DrawImage($image, $x, $y, $width, $height)
    } finally { $image.Dispose() }
    $script:pageIndex++
    $eventArgs.HasMorePages = $script:pageIndex -lt $ImagePaths.Count
})
$document.Print()
""",
            encoding="utf-8",
        )
        printing = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                printer_name or "",
                *map(str, pages),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if printing.returncode != 0:
            raise RuntimeError("Windows image print failed: " + (printing.stderr or printing.stdout).strip())
    finally:
        shutil.rmtree(render_dir, ignore_errors=True)


def print_blank_separator_page(printer_name=None):
    """Print one blank page to separate one booking's bundle from the next."""
    script = """
param([string]$PrinterName)
Add-Type -AssemblyName System.Drawing
$document = New-Object System.Drawing.Printing.PrintDocument
if ($PrinterName) { $document.PrinterSettings.PrinterName = $PrinterName }
$document.add_PrintPage({ param($sender, $eventArgs); $eventArgs.Graphics.Clear([System.Drawing.Color]::White); $eventArgs.HasMorePages = $false })
$document.Print()
"""
    with tempfile.NamedTemporaryFile(
        prefix="arpra-blank-page-", suffix=".ps1", delete=False, mode="w", encoding="utf-8"
    ) as file:
        file.write(script)
        script_path = file.name
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path, printer_name or ""],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError("Blank separator page could not be printed: " + (result.stderr or result.stdout).strip())
    finally:
        Path(script_path).unlink(missing_ok=True)


def print_label_sheet_html(html, printer_name=None):
    """Render label-sheet HTML to PDF on the server and print it to the selected label printer."""
    browser = find_browser_executable()
    if not browser:
        raise RuntimeError("Microsoft Edge/Chrome nahi mila. Label direct print ke liye browser install/path required hai.")

    render_dir = Path(tempfile.mkdtemp(prefix="arpra-label-print-"))
    try:
        html_path = render_dir / "labels.html"
        pdf_path = render_dir / "labels.pdf"
        html_path.write_text(html, encoding="utf-8")
        browser_run = subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                "--run-all-compositor-stages-before-draw",
                "--virtual-time-budget=1500",
                f"--user-data-dir={render_dir / 'profile'}",
                f"--print-to-pdf={pdf_path}",
                html_path.resolve().as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if browser_run.returncode != 0 or not pdf_path.exists():
            raise RuntimeError("Label PDF generate nahi hua: " + (browser_run.stderr or browser_run.stdout).strip())
        print_rendered_pdf(str(pdf_path), printer_name=printer_name)
    finally:
        shutil.rmtree(render_dir, ignore_errors=True)


def eligible_patient(patient_id):
    """Return a current, no-pending-balance Courier/By Hand patient eligible for report access."""
    delivery_clause = (
        "(FIND_IN_SET('C', REPLACE(COALESCE(delivery_mode, ''), ' ', '')) "
        "OR FIND_IN_SET('H', REPLACE(COALESCE(delivery_mode, ''), ' ', '')))"
    )
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT patient_id, patient_name, net_user_pass, charge_as, balance
                FROM patients
                WHERE patient_id = %s
                  AND (charge_as IN ('2', '3') OR (charge_as = '1' AND COALESCE(balance, 0) <= 0))
                  AND {delivery_clause}
                LIMIT 1
                """,
                [patient_id],
            )
            patient = cur.fetchone()
    if patient and has_pending_tests(patient_id):
        return None
    return patient


def fetch_report(patient):
    return requests.get(
        report_url(),
        params={"UserIDPassword": f"{patient['patient_id']},{patient['net_user_pass']}"},
        timeout=(10, 90),
    )


def fetch_receipt(patient_id):
    """Fetch the LabMate receipt PDF using the configured shared receipt password."""
    base_url = "http://10.1.1.178:8000//downloadnew/"
    password = os.getenv("LABMATE_RECEIPT_PASSWORD", "123")
    with requests.Session() as session:
        login_page = session.get(base_url + "PatientLogin.aspx", timeout=(10, 60))
        login_page.raise_for_status()
        soup = BeautifulSoup(login_page.text, "html.parser")
        form_data = {
            item["name"]: item.get("value", "")
            for item in soup.find_all("input", type="hidden")
            if item.get("name")
        }
        form_data.update({
            "txtUserId": str(patient_id),
            "txtPassword": password,
            "btnReceipt": "Print Receipt",
        })
        login_response = session.post(base_url + "PatientLogin.aspx", data=form_data, timeout=(10, 60))
        login_response.raise_for_status()
        receipt_match = re.search(r'''PatReceipt\.aspx\?PValue=([^"']+)["']''', login_response.text)
        if not receipt_match:
            message_match = re.search(r'''alert\(["'](.+?)["']\)''', login_response.text)
            message = message_match.group(1) if message_match else "receipt is unavailable or login was rejected"
            raise RuntimeError("No receipt returned: " + message)
        receipt_response = session.get(
            base_url + "PatReceipt.aspx?PValue=" + receipt_match.group(1),
            timeout=(10, 90),
        )
        receipt_response.raise_for_status()
        is_pdf = receipt_response.content.startswith(b"%PDF") or "pdf" in receipt_response.headers.get("Content-Type", "").lower()
        if not is_pdf:
            raise RuntimeError("Receipt server did not return a PDF.")
        return receipt_response.content


def receipt_cache_path(patient_id):
    return RECEIPT_CACHE_DIR / f"{int(patient_id)}.pdf"


def cached_receipt_pdf(patient_id, force=False):
    """Return a cached receipt PDF, fetching it once when missing or forced."""
    path = receipt_cache_path(patient_id)
    if not force and path.exists() and path.stat().st_size > 0:
        return path.read_bytes()

    RECEIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        pdf_bytes = fetch_receipt(patient_id)
        path.write_bytes(pdf_bytes)
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE patients
                    SET receipt_cached_path=%s, receipt_cached_at=NOW(), receipt_cache_error=NULL
                    WHERE patient_id=%s
                    """,
                    [str(path), patient_id],
                )
            conn.commit()
        return pdf_bytes
    except Exception as exc:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE patients
                    SET receipt_cache_error=%s
                    WHERE patient_id=%s
                    """,
                    [str(exc), patient_id],
                )
            conn.commit()
        raise


def enqueue_receipt_cache(patient_id):
    """Add one patient to the receipt cache queue once, with a hard queue cap."""
    patient_id = int(patient_id)
    with receipt_cache_lock:
        if patient_id in receipt_cache_pending:
            return False
        if len(receipt_cache_queue) >= RECEIPT_CACHE_QUEUE_LIMIT:
            return False
        receipt_cache_queue.append(patient_id)
        receipt_cache_pending.add(patient_id)
    receipt_cache_wakeup.set()
    return True


def enqueue_recent_receipt_cache_candidates(limit=RECEIPT_CACHE_ENQUEUE_LIMIT):
    """Queue recent Paying Courier/By Hand receipts for slow background prefetching."""
    cutoff = (date.today() - timedelta(days=SYNC_LOOKBACK_DAYS - 1)).isoformat()
    local_delivery_match = """
        (FIND_IN_SET('C', REPLACE(COALESCE(delivery_mode, ''), ' ', ''))
         OR FIND_IN_SET('H', REPLACE(COALESCE(delivery_mode, ''), ' ', '')))
    """
    home_delivery_exists = """
        EXISTS (
            SELECT 1
            FROM home_collection_bookings AS hcbp
            WHERE hcbp.booking_id = CAST(SUBSTRING_INDEX(COALESCE(req_slip_no, ''), '-', -1) AS UNSIGNED)
              AND LOWER(COALESCE(hcbp.report_delivery, '')) REGEXP '(^|,)(courier|by_hand)(,|$)'
        )
    """
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT patient_id, receipt_cached_path
                FROM patients
                WHERE charge_as='1'
                  AND COALESCE(balance, 0) <= 0
                  AND booking_date >= %s
                  AND report_printed=0
                  AND receipt_cache_error IS NULL
                  AND ({local_delivery_match} OR (NOT {local_delivery_match} AND {home_delivery_exists}))
                ORDER BY booking_date DESC, patient_id DESC
                LIMIT %s
                """,
                [cutoff, limit],
            )
            rows = cur.fetchall()

    enqueued = 0
    for row in rows:
        cached_value = str(row.get("receipt_cached_path") or "")
        cached_path = Path(cached_value) if cached_value else None
        if cached_path and cached_path.exists() and cached_path.is_file() and cached_path.stat().st_size > 0:
            continue
        if enqueue_receipt_cache(row["patient_id"]):
            enqueued += 1
    if enqueued:
        app.logger.info("Queued %s receipt(s) for background cache.", enqueued)


def enqueue_visible_receipts(patients, limit=RECEIPT_CACHE_ENQUEUE_LIMIT):
    """Queue receipts for currently visible Paying Courier/By Hand patients without blocking the page."""
    enqueued = 0
    for patient in patients:
        if enqueued >= limit:
            break
        if str(patient.get("charge_as") or "") != "1" or patient.get("receipt_cached"):
            continue
        if not patient.get("delivery_label"):
            continue
        if patient.get("payment_due"):
            continue
        if patient.get("receipt_cache_error"):
            continue
        if enqueue_receipt_cache(patient["patient_id"]):
            enqueued += 1
    if enqueued:
        app.logger.info("Queued %s visible receipt(s) for background cache.", enqueued)


@app.get("/patients/<int:patient_id>/receipt")
def view_patient_receipt(patient_id):
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT patient_id, charge_as FROM patients WHERE patient_id=%s LIMIT 1",
                [patient_id],
            )
            patient = cur.fetchone()
    if not patient or str(patient["charge_as"] or "") != "1":
        abort(404)
    try:
        pdf_bytes = cached_receipt_pdf(patient_id)
    except (requests.RequestException, RuntimeError, OSError) as exc:
        app.logger.exception("Receipt preview failed for patient_id=%s", patient_id)
        return Response(f"Receipt could not be fetched: {exc}", status=502, mimetype="text/plain")
    return Response(
        pdf_bytes,
        content_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="patient-receipt-{patient_id}.pdf"'},
    )


@app.get("/patients/<int:patient_id>/report")
def view_patient_report(patient_id):
    """Open the report in the browser without exposing the LabMate password in the URL."""
    patient = eligible_patient(patient_id)
    if not patient or not patient["net_user_pass"]:
        abort(404)
    try:
        response = fetch_report(patient)
        response.raise_for_status()
    except requests.RequestException as exc:
        app.logger.exception("Patient report preview failed for patient_id=%s", patient_id)
        return Response(f"Report could not be fetched: {exc}", status=502, mimetype="text/plain")

    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if response.content.startswith(b"%PDF"):
        content_type = "application/pdf"
    elif not content_type:
        content_type = "text/html"
    filename = f"patient-report-{patient_id}.pdf" if content_type == "application/pdf" else f"patient-report-{patient_id}"
    return Response(
        response.content,
        content_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.post("/patients/<int:patient_id>/print")
def print_patient_report(patient_id):
    """Fetch and print one eligible patient's report without exposing their password to the browser."""
    patient = eligible_patient(patient_id)

    if not patient:
        flash("This patient is no longer eligible for printing.", "error")
        return redirect(url_for("dashboard"))
    if not patient["net_user_pass"]:
        flash("Print could not start: LabMate report password is unavailable.", "error")
        return redirect(url_for("dashboard"))

    try:
        response = fetch_report(patient)
        response.raise_for_status()
        is_pdf = response.content.startswith(b"%PDF") or "application/pdf" in response.headers.get("Content-Type", "").lower()
        if not is_pdf:
            raise RuntimeError("Report server did not return a PDF.")
        report_printer = selected_printer("report")
        receipt_printer = selected_printer("receipt")
        if str(patient["charge_as"] or "") == "1":
            print_pdf(cached_receipt_pdf(patient_id), printer_name=receipt_printer)
        print_pdf(response.content, printer_name=report_printer)
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE patients SET report_printed=1, report_printed_at=NOW() WHERE patient_id=%s",
                    [patient_id],
                )
            conn.commit()
        record_print_attempt(
            patient_id,
            "success",
            "report",
            receipt_included=str(patient["charge_as"] or "") == "1",
        )
        label = "Receipt and report" if str(patient["charge_as"] or "") == "1" else "Report"
        flash(f"{label} print command sent for {patient['patient_name'] or patient_id}.", "success")
    except (requests.RequestException, RuntimeError, OSError) as exc:
        app.logger.exception("Patient report print failed for patient_id=%s", patient_id)
        record_print_attempt(
            patient_id,
            "failed",
            "print",
            receipt_included=str(patient["charge_as"] or "") == "1",
            error_message=str(exc),
        )
        flash(f"Print could not start: {exc}", "error")
    return redirect(url_for("dashboard"))


@app.template_filter("inr")
def inr(value):
    return f"₹{float(value or 0):,.2f}"


@app.route("/")
def dashboard():
    query = request.args.get("q", "").strip()
    delivery = request.args.get("delivery", "all")
    review_filter = request.args.get("review_status", "all")
    if review_filter not in {"all", "unseen", "waiting"}:
        review_filter = "all"
    print_status = request.args.get("print_status", "all")
    if print_status not in {"all", "printed", "not_printed"}:
        print_status = "all"
    selected_date = request.args.get("booking_date", date.today().isoformat())
    try:
        selected_date = datetime.strptime(selected_date, "%Y-%m-%d").date().isoformat()
    except ValueError:
        selected_date = date.today().isoformat()
    # The operations queue intentionally has no pagination.  A patient without
    # a request slip is displayed as its own item, so limiting this query can
    # otherwise hide those items after the first page.
    page = 1

    # C = Courier and H = By Hand in LabMate. For home collections, the
    # numeric suffix of req_slip_no (HCB-2025 → 2025) maps to booking_id.
    local_delivery_match = """
        (FIND_IN_SET('C', REPLACE(COALESCE(delivery_mode, ''), ' ', ''))
         OR FIND_IN_SET('H', REPLACE(COALESCE(delivery_mode, ''), ' ', '')))
    """
    home_delivery_exists = """
        EXISTS (
            SELECT 1
            FROM home_collection_bookings AS hcbp
            WHERE hcbp.booking_id = CAST(SUBSTRING_INDEX(COALESCE(req_slip_no, ''), '-', -1) AS UNSIGNED)
              AND LOWER(COALESCE(hcbp.report_delivery, '')) REGEXP '(^|,)(courier|by_hand)(,|$)'
        )
    """
    clauses = [
        "booking_date = %s",
        f"({local_delivery_match} OR (NOT {local_delivery_match} AND {home_delivery_exists}))"
    ]
    params = [selected_date]
    if delivery in DELIVERY_LABELS:
        clauses.append("FIND_IN_SET(%s, REPLACE(COALESCE(delivery_mode, ''), ' ', ''))")
        params.append(delivery)
    if print_status == "printed":
        clauses.append("report_printed = 1")
    elif print_status == "not_printed":
        clauses.append("report_printed = 0")
    if query:
        like = f"%{query}%"
        clauses.append("(CAST(patient_id AS CHAR) LIKE %s OR patient_name LIKE %s OR mobile_no LIKE %s OR receipt_no LIKE %s)")
        params.extend([like, like, like, like])

    where_sql = " AND ".join(clauses)
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM patients WHERE {where_sql}", params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"""
                SELECT patient_id, bdate, patient_name, mobile_no, company, delivery_mode,
                       urgent_report, net_user_pass, charge_as, manual_approved_print, manual_approval_type,
                       partial_review_status, partial_review_webhook_id, partial_review_completed_hash,
                       partial_review_at, receipt_cached_path, receipt_cached_at, receipt_cache_error,
                       webhook_id, webhook_test_list, webhook_report_path, webhook_report_status,
                       webhook_completed_hash, webhook_synced_at,
                       report_printed, report_printed_at, req_slip_no, req_slip_date, charge,
                       discount, net, receipt, balance, receipt_no, bill_no, source_synced_at AS synced_at
                FROM patients
                WHERE {where_sql}
                ORDER BY patient_id DESC
                """,
                params,
            )
            patients = cur.fetchall()

            patient_ids = [patient["patient_id"] for patient in patients]
            tests_by_patient = {}
            if patient_ids:
                placeholders = ", ".join(["%s"] * len(patient_ids))
                cur.execute(
                    f"""
                    SELECT pt.patient_id, pt.test_code
                    FROM patient_tests AS pt
                    WHERE pt.active = 1 AND pt.patient_id IN ({placeholders})
                    ORDER BY pt.patient_id, pt.test_code
                    """,
                    patient_ids,
                )
                for test in cur.fetchall():
                    catalog_test = CATALOG_PRELOAD.get_test_by_booked_code(test["test_code"])
                    tests_by_patient.setdefault(test["patient_id"], []).append(
                        {
                            "code": test["test_code"],
                            "name": catalog_test.name if catalog_test else test["test_code"],
                            "catalog_test": catalog_test,
                            "is_profile": bool(catalog_test and catalog_test.is_profile),
                        }
                    )

                booking_ids = sorted(
                    {
                        booking_id_from_slip(patient["req_slip_no"])
                        for patient in patients
                        if not has_labmate_courier_delivery(patient["delivery_mode"])
                    }
                    - {None}
                )
                home_delivery_by_booking = {}
                if booking_ids:
                    home_placeholders = ", ".join(["%s"] * len(booking_ids))
                    cur.execute(
                        f"""
                        SELECT booking_id, report_delivery
                        FROM home_collection_bookings
                        WHERE booking_id IN ({home_placeholders})
                        GROUP BY booking_id
                        """,
                        booking_ids,
                    )
                    home_delivery_by_booking = {
                        row["booking_id"]: home_delivery_label(row["report_delivery"])
                        for row in cur.fetchall()
                    }

                completed_by_patient = {}
            else:
                completed_by_patient = {}
                home_delivery_by_booking = {}

    completed_by_patient = {
        patient["patient_id"]: completed_test_codes(patient.get("webhook_test_list"))
        for patient in patients
        if patient.get("webhook_id")
    }
    latest_webhook_id_by_patient = {
        patient["patient_id"]: patient.get("webhook_id")
        for patient in patients
        if patient.get("webhook_id")
    }
    report_path_by_patient = {
        patient["patient_id"]: str(patient.get("webhook_report_path") or "").strip()
        for patient in patients
        if patient.get("webhook_id")
    }
    report_status_by_patient = {
        patient["patient_id"]: str(patient.get("webhook_report_status") or "").strip()
        for patient in patients
        if patient.get("webhook_id")
    }

    missing_webhook_ids = [
        patient_id for patient_id in patient_ids if patient_id not in latest_webhook_id_by_patient
    ]
    if missing_webhook_ids:
        # Fallback keeps manually-opened pages correct while the 3-second
        # background sync catches up, and also covers older selected dates.
        latest_webhooks = fetch_latest_webhooks(missing_webhook_ids)
        for patient_id, webhook in latest_webhooks.items():
            completed_by_patient[patient_id] = webhook["completed_codes"]
            latest_webhook_id_by_patient[patient_id] = webhook["id"]
            report_path_by_patient[patient_id] = webhook["report_path"]
            report_status_by_patient[patient_id] = webhook["report_status"]

    stale_partial_patient_ids = []
    for patient in patients:
        patient["is_labmate_delivery"] = has_labmate_courier_delivery(patient["delivery_mode"])
        labmate_delivery = delivery_label(patient["delivery_mode"])
        home_delivery = ""
        if not patient["is_labmate_delivery"]:
            home_delivery = home_delivery_by_booking.get(booking_id_from_slip(patient["req_slip_no"]), "")
        patient["delivery_label"] = labmate_delivery if patient["is_labmate_delivery"] else home_delivery
        patient["home_delivery_label"] = home_delivery
        patient["tests"] = tests_by_patient.get(patient["patient_id"], [])
        patient["report_path"] = report_path_by_patient.get(patient["patient_id"], "")
        completed_codes = completed_by_patient.get(patient["patient_id"], set())
        patient["latest_webhook_id"] = latest_webhook_id_by_patient.get(patient["patient_id"])
        patient["completed_tests_hash"] = completed_tests_hash(completed_codes)
        for test in patient["tests"]:
            catalog_test = test["catalog_test"]
            test["children"] = catalog_child_tree(catalog_test, completed_codes) if test["is_profile"] else []
            test["is_completed"] = is_catalog_test_completed(test["code"], completed_codes)
        patient["completed_tests"] = sum(test["is_completed"] for test in patient["tests"])
        patient["pending_tests"] = len(patient["tests"]) - patient["completed_tests"]
        patient["needs_partial_review"] = (
            patient["completed_tests"] > 0
            and patient["pending_tests"] > 0
            and not patient["report_printed"]
            and not patient["manual_approved_print"]
        )
        patient["report_status"] = report_status_by_patient.get(patient["patient_id"], "")
        patient["completion_state"] = completion_state(
            patient["report_status"], patient["completed_tests"]
        )
        patient_review_status = str(patient.get("partial_review_status") or "").strip()
        saved_webhook_id = patient.get("partial_review_webhook_id")
        saved_hash = str(patient.get("partial_review_completed_hash") or "")
        partial_changed = (
            patient["needs_partial_review"]
            and patient_review_status in {"wait", "ready"}
            and saved_webhook_id != patient["latest_webhook_id"]
            and saved_hash != patient["completed_tests_hash"]
        )
        if partial_changed:
            stale_partial_patient_ids.append(patient["patient_id"])
            patient["partial_review_status"] = "unseen"
            patient["partial_review_webhook_id"] = patient["latest_webhook_id"]
            patient["partial_review_completed_hash"] = patient["completed_tests_hash"]
            patient["manual_approved_print"] = 0
            patient["manual_approval_type"] = None
        elif patient["needs_partial_review"]:
            patient["partial_review_status"] = (
                patient_review_status
                if patient_review_status in {"wait", "ready"}
                else "unseen"
            )
        else:
            patient["partial_review_status"] = ""
        patient["charge_mode_label"] = charge_mode_label(patient["charge_as"])
        patient["payment_due"] = (
            str(patient["charge_as"] or "") == "1"
            and Decimal(str(patient["balance"] or 0)) > 0
        )
        receipt_path = Path(str(patient.get("receipt_cached_path") or ""))
        patient["receipt_cached"] = (
            str(patient["charge_as"] or "") == "1"
            and bool(patient.get("receipt_cached_path"))
            and receipt_path.exists()
            and receipt_path.is_file()
        )
        patient["can_access_report"] = (
            bool(patient["tests"])
            and can_access_report(patient["charge_as"], patient["balance"])
            and patient["pending_tests"] == 0
        ) or bool(patient["manual_approved_print"])
        if patient["can_access_report"] and patient["report_path"]:
            patient["direct_report_url"] = patient["report_path"]
        else:
            patient["direct_report_url"] = None
        patient["bdate_display"] = scalar(patient["bdate"])
        patient["synced_at_display"] = scalar(patient["synced_at"])

    enqueue_visible_receipts(patients)

    if stale_partial_patient_ids:
        placeholders = ", ".join(["%s"] * len(stale_partial_patient_ids))
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE patients
                    SET manual_approved_print=0,
                        manual_approval_type=NULL,
                        partial_review_status='unseen',
                        partial_review_webhook_id=NULL,
                        partial_review_completed_hash=NULL,
                        partial_review_at=NOW()
                    WHERE patient_id IN ({placeholders})
                    """,
                    stale_partial_patient_ids,
                )
            conn.commit()

    dispatch_groups = []
    groups_by_slip = {}
    for patient in patients:
        slip_no = str(patient["req_slip_no"] or "").strip()
        # Missing slip numbers must not join unrelated patients into one group.
        group_key = slip_no or f"patient-{patient['patient_id']}"
        if group_key not in groups_by_slip:
            group = {"slip_no": slip_no, "group_key": group_key, "patients": []}
            groups_by_slip[group_key] = group
            dispatch_groups.append(group)
        groups_by_slip[group_key]["patients"].append(patient)

    for group in dispatch_groups:
        group["patients"].sort(key=lambda patient: int(patient["patient_id"]), reverse=True)
        group["sort_patient_id"] = max(int(patient["patient_id"]) for patient in group["patients"])
    dispatch_groups.sort(key=lambda group: group["sort_patient_id"], reverse=True)

    envelope_by_key = {}
    if groups_by_slip:
        envelope_keys = list(groups_by_slip.keys())
        placeholders = ", ".join(["%s"] * len(envelope_keys))
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT booking_key, envelope_id, status, printed_at,
                           reprint_count, latest_reprint_at
                    FROM courier_envelopes
                    WHERE booking_key IN ({placeholders})
                    """,
                    envelope_keys,
                )
                envelope_by_key = {row["booking_key"]: row for row in cur.fetchall()}

    for group in dispatch_groups:
        group["label"] = envelope_by_key.get(group["group_key"])
        group["label_badge"] = ""
        if group["label"]:
            if group["label"]["status"] == "reprinted":
                group["label_badge"] = "Label reprinted"
            elif group["label"]["status"] == "printed":
                group["label_badge"] = "Label printed"
        group["all_printable"] = bool(group["patients"]) and all(p["direct_report_url"] for p in group["patients"])
        group["is_printed"] = bool(group["patients"]) and all(
            patient["report_printed"] for patient in group["patients"]
        )
        group["patient_ids"] = ",".join(str(p["patient_id"]) for p in group["patients"])
        group["has_manual_review"] = any(
            patient["manual_approved_print"] for patient in group["patients"]
        )
        group["all_manual_approved"] = bool(group["patients"]) and all(
            patient["manual_approved_print"] for patient in group["patients"]
        )
        group["batch_selectable"] = group["all_printable"] or group["all_manual_approved"]
        group["has_further_action"] = any(
            patient["manual_approval_type"] == "further_action"
            for patient in group["patients"]
        )
        states = {patient["completion_state"] for patient in group["patients"]}
        # A manual approval supplies a verified report URL, so once every
        # patient in the booking is printable the booking belongs in Ready.
        if group["is_printed"]:
            group["completion_state"] = "printed"
        elif group["all_printable"]:
            group["completion_state"] = "ready"
        elif states == {"not-complete"}:
            group["completion_state"] = "not-complete"
        else:
            group["completion_state"] = "partial"

        for patient in group["patients"]:
            patient["needs_partial_review"] = (
                group["completion_state"] == "partial"
                and patient["pending_tests"] > 0
                and not patient["report_printed"]
                and not patient["manual_approved_print"]
            )
            if not patient["needs_partial_review"]:
                continue
            patient_review_status = str(patient.get("partial_review_status") or "").strip()
            if (
                patient_review_status in {"wait", "ready"}
                and patient.get("partial_review_webhook_id") != patient["latest_webhook_id"]
                and str(patient.get("partial_review_completed_hash") or "") != patient["completed_tests_hash"]
            ):
                stale_partial_patient_ids.append(patient["patient_id"])
                patient["partial_review_status"] = "unseen"
                patient["partial_review_webhook_id"] = patient["latest_webhook_id"]
                patient["partial_review_completed_hash"] = patient["completed_tests_hash"]
                patient["manual_approved_print"] = 0
                patient["manual_approval_type"] = None
            elif patient_review_status not in {"wait", "ready"}:
                patient["partial_review_status"] = "unseen"

        group_partial_statuses = {
            patient["partial_review_status"]
            for patient in group["patients"]
            if patient.get("needs_partial_review")
        }
        if "wait" in group_partial_statuses:
            group["partial_review_badge"] = "Waiting"
        elif "unseen" in group_partial_statuses:
            group["partial_review_badge"] = "Unseen"
        else:
            group["partial_review_badge"] = ""

    if stale_partial_patient_ids:
        placeholders = ", ".join(["%s"] * len(set(stale_partial_patient_ids)))
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE patients
                    SET manual_approved_print=0,
                        manual_approval_type=NULL,
                        partial_review_status='unseen',
                        partial_review_webhook_id=NULL,
                        partial_review_completed_hash=NULL,
                        partial_review_at=NOW()
                    WHERE patient_id IN ({placeholders})
                    """,
                    list(set(stale_partial_patient_ids)),
                )
            conn.commit()

    all_dispatch_groups = dispatch_groups
    total_groups = len(all_dispatch_groups)
    needs_review_count = sum(not group["all_printable"] for group in all_dispatch_groups)
    printed_today_count = sum(patient["report_printed"] for patient in patients)
    completion_counts = {
        "manual": sum(group["completion_state"] == "partial" for group in all_dispatch_groups),
        "not-complete": sum(
            group["completion_state"] == "not-complete" for group in all_dispatch_groups
        ),
        "ready": sum(group["completion_state"] == "ready" for group in all_dispatch_groups),
        "printed": sum(group["completion_state"] == "printed" for group in all_dispatch_groups),
    }

    if review_filter in {"unseen", "waiting"}:
        expected_badge = "Unseen" if review_filter == "unseen" else "Waiting"
        dispatch_groups = [
            group for group in all_dispatch_groups
            if group.get("partial_review_badge") == expected_badge
        ]

    total_pages = 1
    return render_template(
        "dashboard.html",
        patients=patients,
        dispatch_groups=dispatch_groups,
        total=total_groups,
        total_groups=total_groups,
        needs_review_count=needs_review_count,
        printed_today_count=printed_today_count,
        courier_count=sum("C" in (p["delivery_mode"] or "").split(",") for p in patients),
        by_hand_count=sum("H" in (p["delivery_mode"] or "").split(",") for p in patients),
        query=query,
        delivery=delivery,
        review_status=review_filter,
        print_status=print_status,
        completion_counts=completion_counts,
        selected_date=selected_date,
        page=page,
        total_pages=total_pages,
    )


@app.post("/patients/<int:patient_id>/manual-decision/<decision>")
def manual_print_decision(patient_id, decision):
    if decision != "approve":
        abort(400)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    approval_type = request.args.get("approval_type", "no_further_action")
    if approval_type not in {"no_further_action", "further_action"}:
        abort(400)
    report_path = ""
    if decision == "approve":
        with pymysql.connect(
            host=os.getenv("LABMATE_DB_HOST", "10.1.1.44"), port=3306,
            user=os.getenv("LABMATE_DB_USER", "sahil"), password=os.environ["LABMATE_DB_PASSWORD"],
            database="lead_management", cursorclass=pymysql.cursors.DictCursor,
        ) as source:
            with source.cursor() as cur:
                cur.execute(
                    "SELECT r_without_lh FROM labmate_webhook_logs WHERE patient_id=%s ORDER BY id DESC LIMIT 1",
                    [patient_id],
                )
                latest_webhook = cur.fetchone()
        report_path = str((latest_webhook or {}).get("r_without_lh") or "").strip()
        if not report_path:
            message = "The latest webhook entry has no report URL. This patient was not approved."
            if is_ajax:
                return jsonify({"ok": False, "message": message}), 409
            flash(message, "error")
            return redirect(request.referrer or url_for("dashboard"))
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE patients
                SET manual_approved_print=1, manual_approval_type=%s, manual_decision_at=NOW()
                WHERE patient_id=%s
                """,
                [approval_type, patient_id],
            )
        conn.commit()
    message = "Further action approved for print." if approval_type == "further_action" else "Approved for print."
    if is_ajax:
        return jsonify({"ok": True, "approved": True, "approval_type": approval_type, "report_url": report_path, "message": message})
    flash(message, "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/patients/<int:patient_id>/partial-review/<decision>")
def partial_review_decision(patient_id, decision):
    if decision not in {"wait", "ready"}:
        abort(400)
    approval_type = request.args.get("approval_type", "no_further_action")
    if decision == "ready" and approval_type not in {"no_further_action", "further_action"}:
        abort(400)
    webhook = latest_webhook_for_patient(patient_id)
    if decision == "ready" and not webhook["id"]:
        return jsonify({"ok": False, "message": "No webhook entry was found for this patient."}), 409
    if decision == "ready" and not webhook["report_path"]:
        return jsonify({"ok": False, "message": "The latest webhook entry has no report URL. This patient was not approved."}), 409

    status = "ready" if decision == "ready" else "wait"
    with db_connection() as conn:
        with conn.cursor() as cur:
            if decision == "ready":
                cur.execute(
                    """
                    UPDATE patients
                    SET manual_approved_print=1,
                        manual_approval_type=%s,
                        manual_decision_at=NOW(),
                        partial_review_status=%s,
                        partial_review_webhook_id=%s,
                        partial_review_completed_hash=%s,
                        partial_review_at=NOW()
                    WHERE patient_id=%s
                    """,
                    [approval_type, status, webhook["id"], webhook["completed_hash"], patient_id],
                )
            else:
                cur.execute(
                    """
                    UPDATE patients
                    SET partial_review_status=%s,
                        partial_review_webhook_id=%s,
                        partial_review_completed_hash=%s,
                        partial_review_at=NOW()
                    WHERE patient_id=%s
                    """,
                    [status, webhook["id"], webhook["completed_hash"], patient_id],
                )
        conn.commit()

    return jsonify(
        {
            "ok": True,
            "status": status,
            "approval_type": approval_type if status == "ready" else None,
            "report_url": webhook["report_path"],
            "message": "Ready to print." if status == "ready" else "Marked as wait.",
        }
    )


@app.post("/print-selected-bookings")
def print_selected_bookings():
    """Print receipt then report for each patient, with blank pages between bookings."""
    raw_groups = request.form.get("patient_ids", "")
    try:
        booking_groups = []
        seen_ids = set()
        for raw_group in raw_groups.split(";"):
            group_ids = [int(value) for value in raw_group.split(",") if value.strip()]
            if not group_ids:
                continue
            if any(patient_id in seen_ids for patient_id in group_ids):
                abort(400)
            seen_ids.update(group_ids)
            booking_groups.append(group_ids)
        patient_ids = [patient_id for group in booking_groups for patient_id in group]
    except ValueError:
        abort(400)
    if not patient_ids:
        flash("Select at least one ready booking.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    placeholders = ", ".join(["%s"] * len(patient_ids))
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT patient_id, req_slip_no, charge_as, balance, manual_approved_print, report_printed FROM patients WHERE patient_id IN ({placeholders})",
                patient_ids,
            )
            local_patients = {int(row["patient_id"]): row for row in cur.fetchall()}
    ineligible = []
    for patient_id in patient_ids:
        patient = local_patients.get(patient_id)
        eligible = patient and (
            bool(patient["manual_approved_print"])
            or (can_access_report(patient["charge_as"], patient["balance"]) and not has_pending_tests(patient_id))
        )
        if not eligible:
            ineligible.append(str(patient_id))
    if ineligible:
        flash("This selection contains a patient that is not currently approved/complete: " + ", ".join(ineligible) + ". Nothing was printed.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    with pymysql.connect(
        host=os.getenv("LABMATE_DB_HOST", "10.1.1.44"), port=3306,
        user=os.getenv("LABMATE_DB_USER", "sahil"), password=os.environ["LABMATE_DB_PASSWORD"],
        database="lead_management", cursorclass=pymysql.cursors.DictCursor,
    ) as source:
        with source.cursor() as cur:
            cur.execute(
                f"""
                SELECT webhook.patient_id, webhook.r_without_lh
                FROM labmate_webhook_logs AS webhook
                INNER JOIN (
                    SELECT patient_id, MAX(id) AS latest_id
                    FROM labmate_webhook_logs
                    WHERE patient_id IN ({placeholders})
                    GROUP BY patient_id
                ) AS latest ON latest.latest_id = webhook.id
                """,
                patient_ids,
            )
            paths = {int(row["patient_id"]): str(row["r_without_lh"] or "").strip() for row in cur.fetchall()}

    missing = [str(patient_id) for patient_id in patient_ids if not paths.get(patient_id)]
    if missing:
        flash("This selection changed: report path is unavailable for patient " + ", ".join(missing) + ". Nothing was printed.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO print_batches (total_patients) VALUES (%s)", [len(patient_ids)])
            batch_id = cur.lastrowid
        conn.commit()

    printed_ids = []
    current_patient_id = None
    current_stage = "report"
    report_printer = selected_printer("report")
    receipt_printer = selected_printer("receipt")
    try:
        for group_index, booking_group in enumerate(booking_groups):
            for patient_id in booking_group:
                current_patient_id = patient_id
                # Paying patients get receipt first; Free/Credit patients get
                # their report only.
                if str(local_patients[patient_id]["charge_as"] or "") == "1":
                    current_stage = "receipt"
                    print_pdf(cached_receipt_pdf(patient_id), printer_name=receipt_printer)
                current_stage = "report"
                response = requests.get(paths[patient_id], timeout=(10, 90))
                response.raise_for_status()
                if not response.content.startswith(b"%PDF"):
                    raise RuntimeError(f"Patient {patient_id} report is not a PDF.")
                print_pdf(response.content, printer_name=report_printer)
                printed_ids.append(patient_id)
                with db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE patients SET report_printed=1, report_printed_at=NOW() WHERE patient_id=%s",
                            [patient_id],
                        )
                    conn.commit()
                record_print_attempt(
                    patient_id,
                    "success",
                    "report",
                    receipt_included=str(local_patients[patient_id]["charge_as"] or "") == "1",
                    batch_id=batch_id,
                    booking_label=str(local_patients[patient_id]["req_slip_no"] or "Unknown group"),
                )
            if group_index < len(booking_groups) - 1:
                print_blank_separator_page(printer_name=report_printer)
    except (requests.RequestException, RuntimeError, OSError) as exc:
        app.logger.exception("Batch print failed after patient IDs=%s", printed_ids)
        if current_patient_id is not None:
            record_print_attempt(
                current_patient_id,
                "failed",
                current_stage,
                receipt_included=str(local_patients[current_patient_id]["charge_as"] or "") == "1",
                error_message=str(exc),
                batch_id=batch_id,
                booking_label=str(local_patients[current_patient_id]["req_slip_no"] or "Unknown group"),
            )
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE print_batches SET status='failed', completed_patients=%s, failed_patients=1, error_message=%s, finished_at=NOW() WHERE id=%s",
                    [len(printed_ids), str(exc), batch_id],
                )
            conn.commit()
        flash(f"Printing stopped: {exc}. Successfully completed: {len(printed_ids)} patient bundle(s).", "error")
        return redirect(request.referrer or url_for("dashboard"))

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE print_batches SET status='success', completed_patients=%s, finished_at=NOW() WHERE id=%s",
                [len(printed_ids), batch_id],
            )
        conn.commit()
    flash(f"Print commands sent for {len(printed_ids)} patient bundle(s). Receipts were included for Paying patients only.", "success")
    return redirect(request.referrer or url_for("dashboard"))


def selected_booking_groups_from_form():
    raw_groups = request.form.get("patient_ids", "")
    booking_groups = []
    seen_ids = set()
    for raw_group in raw_groups.split(";"):
        group_ids = [int(value) for value in raw_group.split(",") if value.strip()]
        if not group_ids:
            continue
        if any(patient_id in seen_ids for patient_id in group_ids):
            abort(400)
        seen_ids.update(group_ids)
        booking_groups.append(group_ids)
    if not booking_groups:
        flash("Select at least one booking.", "error")
        return []
    patient_ids = [patient_id for group in booking_groups for patient_id in group]
    placeholders = ", ".join(["%s"] * len(patient_ids))
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT patient_id, booking_date, patient_name, mobile_no, req_slip_no, delivery_mode,
                           charge_as, receipt_cached_path
                FROM patients
                WHERE patient_id IN ({placeholders})
                """,
                patient_ids,
            )
            patients_by_id = {int(row["patient_id"]): row for row in cur.fetchall()}
    groups = []
    for group_ids in booking_groups:
        patients = [patients_by_id[patient_id] for patient_id in group_ids if patient_id in patients_by_id]
        if patients:
            patients.sort(key=lambda patient: int(patient["patient_id"]), reverse=True)
            groups.append(patients)
    return groups


@app.post("/print-labels")
def print_labels():
    """Create/reuse booking-wise courier envelopes and print an A4 label sheet on the server."""
    start_position = 1
    groups = selected_booking_groups_from_form()
    if not groups:
        return redirect(request.referrer or url_for("dashboard"))
    envelopes = [get_or_create_courier_envelope(group) for group in groups]
    label_printer = selected_printer("label")
    html = render_template(
        "courier_labels.html",
        envelopes=envelopes,
        start_position=start_position,
        label_printer=label_printer,
        direct_print=True,
    )
    try:
        print_label_sheet_html(html, printer_name=label_printer)
    except Exception as exc:
        app.logger.exception("Label print failed")
        flash(f"Label print failed: {exc}", "error")
        return redirect(request.referrer or url_for("dashboard"))

    with db_connection() as conn:
        with conn.cursor() as cur:
            for envelope in envelopes:
                if envelope["status"] == "printed":
                    cur.execute(
                        """
                        UPDATE courier_envelopes
                        SET status='reprinted',
                            reprint_count=reprint_count+1,
                            latest_reprint_at=NOW()
                        WHERE id=%s
                        """,
                        [envelope["id"]],
                    )
                else:
                    cur.execute(
                        """
                        UPDATE courier_envelopes
                        SET status='printed',
                            printed_at=NOW(),
                            printed_by=%s
                        WHERE id=%s
                        """,
                        [
                            (session.get("auth_user") or {}).get("name")
                            or os.getenv("USERNAME")
                            or os.getenv("USER")
                            or "operator",
                            envelope["id"],
                        ],
                    )
        conn.commit()
    target_printer = label_printer or "Windows default printer"
    flash(f"{len(envelopes)} label(s) sent to {target_printer}.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.get("/export-printed-labels.xlsx")
def export_printed_labels():
    """Export one representative patient row for each printed/reprinted label."""
    selected_date = request.args.get("booking_date", date.today().isoformat()).strip()
    from_text = request.args.get("from_date", selected_date).strip() or selected_date
    to_text = request.args.get("to_date", from_text).strip() or from_text
    try:
        from_date = datetime.strptime(from_text, "%Y-%m-%d").date()
    except ValueError:
        from_date = date.today()
    try:
        to_date = datetime.strptime(to_text, "%Y-%m-%d").date()
    except ValueError:
        to_date = from_date
    if from_date > to_date:
        from_date, to_date = to_date, from_date

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT envelope_id, booking_date, recipient_name, recipient_address,
                       mobile_no, patient_ids
                FROM courier_envelopes
                WHERE status IN ('printed', 'reprinted')
                  AND booking_date BETWEEN %s AND %s
                ORDER BY booking_date, envelope_id
                """,
                [from_date, to_date],
            )
            envelopes = cur.fetchall()

            representative_ids = []
            for envelope in envelopes:
                first_id = str(envelope.get("patient_ids") or "").split(",", 1)[0].strip()
                if first_id.isdigit():
                    representative_ids.append(int(first_id))

            patients_by_id = {}
            if representative_ids:
                placeholders = ", ".join(["%s"] * len(representative_ids))
                cur.execute(
                    f"""
                    SELECT patient_id, patient_name, mobile_no
                    FROM patients
                    WHERE patient_id IN ({placeholders})
                    """,
                    representative_ids,
                )
                patients_by_id = {int(row["patient_id"]): row for row in cur.fetchall()}

    rows = []
    for envelope in envelopes:
        first_id_text = str(envelope.get("patient_ids") or "").split(",", 1)[0].strip()
        first_id = int(first_id_text) if first_id_text.isdigit() else None
        patient = patients_by_id.get(first_id) if first_id else None
        booking_date = envelope.get("booking_date") or from_date
        if isinstance(booking_date, datetime):
            booking_date = booking_date.date()
        date_text = booking_date.strftime("%d-%m-%Y") if hasattr(booking_date, "strftime") else str(booking_date)
        rows.append(
            [
                date_text,
                first_id_text,
                patient.get("patient_name") if patient else envelope.get("recipient_name"),
                patient.get("mobile_no") if patient and patient.get("mobile_no") else envelope.get("mobile_no"),
                envelope.get("recipient_address") or "",
                envelope.get("envelope_id"),
            ]
        )

    headers = ["Date", "Patient ID", "Patient Name", "Mobile", "Father/Name", "Envelope ID"]
    workbook = build_simple_xlsx(
        headers,
        rows,
        column_widths={1: 12, 2: 14, 3: 24, 4: 15, 5: 120, 6: 20},
        sheet_name="Printed Labels",
    )
    if from_date == to_date:
        filename = f"printed-labels-{from_date.strftime('%Y%m%d')}.xlsx"
    else:
        filename = f"printed-labels-{from_date.strftime('%Y%m%d')}-{to_date.strftime('%Y%m%d')}.xlsx"
    return Response(
        workbook,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def parse_scanned_envelope_ids(raw_text):
    """Parse comma/newline/scanner-separated envelope IDs while preserving order."""
    seen = set()
    envelope_ids = []
    for value in re.split(r"[\s,;]+", raw_text or ""):
        envelope_id = value.strip().upper()
        if not envelope_id or envelope_id in seen:
            continue
        seen.add(envelope_id)
        envelope_ids.append(envelope_id)
    return envelope_ids


@app.route("/scan-labels", methods=["GET", "POST"])
def scan_labels():
    """Bulk save scanned envelope IDs for label verification."""
    if request.method == "POST":
        raw_text = request.form.get("scan_ids", "")
        envelope_ids = parse_scanned_envelope_ids(raw_text)
        if not envelope_ids:
            flash("Scan at least one envelope ID.", "error")
            return redirect(url_for("scan_labels"))

        scan_batch_id = uuid.uuid4().hex[:12]
        user = session.get("auth_user") or {}
        scanned_by_user_id = str(user.get("id") or "") or None
        scanned_by_name = str(user.get("name") or "Unknown user").strip()
        placeholders = ", ".join(["%s"] * len(envelope_ids))
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT envelope_id
                    FROM label_scan_verifications
                    WHERE envelope_id IN ({placeholders})
                    """,
                    envelope_ids,
                )
                already_scanned = {row["envelope_id"] for row in cur.fetchall()}

                cur.execute(
                    f"""
                    SELECT envelope_id, replaced_by_envelope_id
                    FROM voided_courier_envelopes
                    WHERE envelope_id IN ({placeholders})
                    """,
                    envelope_ids,
                )
                voided_envelopes = {row["envelope_id"]: row for row in cur.fetchall()}

                cur.execute(
                    f"""
                    SELECT envelope_id, booking_key, booking_label
                    FROM courier_envelopes
                    WHERE envelope_id IN ({placeholders})
                    """,
                    envelope_ids,
                )
                envelopes = {row["envelope_id"]: row for row in cur.fetchall()}

                rows_to_insert = []
                rejected_scans = []
                for envelope_id in envelope_ids:
                    if envelope_id in voided_envelopes:
                        replacement = voided_envelopes[envelope_id].get("replaced_by_envelope_id")
                        rejected_scans.append(
                            {
                                "envelope_id": envelope_id,
                                "reason": f"Old label ID. Use latest ID {replacement}." if replacement else "Old label ID.",
                            }
                        )
                        continue
                    if envelope_id not in envelopes:
                        rejected_scans.append(
                            {
                                "envelope_id": envelope_id,
                                "reason": "Label ID not found.",
                            }
                        )
                        continue
                    if envelope_id in already_scanned:
                        continue
                    envelope = envelopes.get(envelope_id)
                    rows_to_insert.append(
                        [
                            scan_batch_id,
                            envelope_id,
                            envelope.get("booking_key") if envelope else None,
                            envelope.get("booking_label") if envelope else None,
                            scanned_by_user_id,
                            scanned_by_name,
                            "verified",
                            raw_text,
                        ]
                    )
                if rows_to_insert:
                    cur.executemany(
                        """
                        INSERT INTO label_scan_verifications
                            (scan_batch_id, envelope_id, booking_key, booking_label,
                             scanned_by_user_id, scanned_by_name, status, raw_batch_text)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        rows_to_insert,
                    )
            conn.commit()

        verified_count = sum(
            1
            for envelope_id in envelope_ids
            if envelope_id not in already_scanned
            and envelope_id in envelopes
            and envelope_id not in voided_envelopes
        )
        duplicate_count = len([envelope_id for envelope_id in envelope_ids if envelope_id in already_scanned])
        rejected_count = len(rejected_scans)
        session["last_label_scan_rejections"] = rejected_scans[:50]
        flash(
            f"Saved {verified_count} verified label(s), {rejected_count} rejected, {duplicate_count} already scanned.",
            "success" if verified_count else "error",
        )
        return redirect(url_for("scan_labels"))

    rejected_scans = session.pop("last_label_scan_rejections", [])
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT scan.envelope_id, scan.booking_label, scan.scanned_by_name,
                       scan.scanned_at, scan.status, envelope.recipient_name
                FROM label_scan_verifications AS scan
                LEFT JOIN courier_envelopes AS envelope ON envelope.envelope_id=scan.envelope_id
                ORDER BY scan.scanned_at DESC, scan.id DESC
                LIMIT 200
                """
            )
            scans = cur.fetchall()
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(status='verified'), 0) AS verified_count,
                    COALESCE(SUM(status='unknown'), 0) AS unknown_count,
                    COUNT(*) AS total_count
                FROM label_scan_verifications
                """
            )
            totals = cur.fetchone() or {}
    return render_template("scan_labels.html", scans=scans, totals=totals, rejected_scans=rejected_scans)


@app.get("/print-batches")
def print_batches():
    """Show only fully successful print batches and their booking/patient records."""
    selected_print_date = request.args.get("print_date", date.today().isoformat())
    try:
        selected_print_date = datetime.strptime(selected_print_date, "%Y-%m-%d").date().isoformat()
    except ValueError:
        selected_print_date = date.today().isoformat()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM print_batches
                WHERE status='success' AND DATE(started_at)=%s
                ORDER BY started_at DESC LIMIT 200
                """,
                [selected_print_date],
            )
            batches = list(cur.fetchall())
            cur.execute(
                """
                SELECT attempt.batch_id, attempt.booking_label, attempt.patient_id, patient.patient_name,
                       attempt.receipt_included, attempt.attempted_at
                FROM print_attempts AS attempt
                LEFT JOIN patients AS patient ON patient.patient_id=attempt.patient_id
                INNER JOIN print_batches AS batch ON batch.id=attempt.batch_id AND batch.status='success'
                WHERE attempt.status='success' AND DATE(attempt.attempted_at)=%s
                ORDER BY attempt.attempted_at DESC
                LIMIT 500
                """,
                [selected_print_date],
            )
            attempts = list(cur.fetchall())
            # Print tracking existed before batch audit tracking was introduced.
            # Include those already-printed local patients so Print batches does
            # not hide historical courier bookings such as HC26-15599.
            cur.execute(
                """
                SELECT NULL AS batch_id,
                       COALESCE(NULLIF(patient.req_slip_no, ''), 'Unknown group') AS booking_label,
                       patient.patient_id, patient.patient_name,
                       0 AS receipt_included, patient.report_printed_at AS attempted_at
                FROM patients AS patient
                WHERE patient.report_printed=1
                  AND DATE(patient.report_printed_at)=%s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM print_attempts AS attempt
                      INNER JOIN print_batches AS batch ON batch.id=attempt.batch_id AND batch.status='success'
                      WHERE attempt.patient_id=patient.patient_id AND attempt.status='success'
                  )
                ORDER BY patient.report_printed_at DESC
                """,
                [selected_print_date],
            )
            historical_attempts = list(cur.fetchall())
            attempts.extend(historical_attempts)
            if historical_attempts:
                # One visual group for prints recorded before batch IDs existed.
                batches.append({"id": "historical"})
    return render_template(
        "print_audit.html",
        mode="batches",
        batches=batches,
        attempts=attempts,
        selected_print_date=selected_print_date,
    )


@app.get("/print-history")
def print_history():
    """Show every recorded print outcome, including failed and ungrouped attempts."""
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM print_batches ORDER BY started_at DESC LIMIT 200")
            batches = cur.fetchall()
            cur.execute(
                """
                SELECT attempt.batch_id, attempt.booking_label, attempt.patient_id, patient.patient_name,
                       attempt.status, attempt.stage, attempt.receipt_included, attempt.error_message,
                       attempt.attempted_at
                FROM print_attempts AS attempt
                LEFT JOIN patients AS patient ON patient.patient_id=attempt.patient_id
                ORDER BY attempt.attempted_at DESC
                LIMIT 500
                """
            )
            attempts = cur.fetchall()
    return render_template("print_audit.html", mode="history", batches=batches, attempts=attempts)


if __name__ == "__main__":
    # Bind to all local adapters so other devices on the same LAN can reach it.
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    # Flask's debug reloader starts a parent and child process. Only the child
    # runs the sync worker, preventing duplicate remote syncs.
    if not debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_receipt_cache_worker()
        start_current_date_webhook_sync_worker()
        start_remote_sync_worker()
    app.run(
        debug=debug_mode,
        host="0.0.0.0",
        port=int(os.getenv("APP_PORT", "5012")),
    )
