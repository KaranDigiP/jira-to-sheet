import os
import json
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import re

# ==============================
# 🔐 CONFIG
# ==============================

JIRA_URL = "https://orangeworking.atlassian.net"
EMAIL = os.getenv("JIRA_EMAIL")
API_TOKEN = os.getenv("JIRA_API_TOKEN")

GOOGLE_SHEET_NAME = "SuperApi-updated-cluster-ticket-sheet"

# SAFE JQL (bounded)
JQL = "project = MDRS AND created >= -365d ORDER BY created DESC"

# ==============================
# 📊 COLUMNS
# ==============================

COLUMNS = [
    "Ticket No", "CVE Names", "CVE ID", "Severity", "Package",
    "Image Version", "Fix Available", "Ticket Link", "Date",
    "Status", "Note", "Image Current Version",
    "Jira Update ticket", "Timeline", "Month",
    "Cluster", "Environment"
]

# ==============================
# 📥 FETCH JIRA DATA
# ==============================

def fetch_jira_issues(jql):
    issues = []
    start_at = 0

    print("EMAIL:", EMAIL)
    print("TOKEN PRESENT:", bool(API_TOKEN))

    while True:
        response = requests.post(
            f"{JIRA_URL}/rest/api/3/search/jql",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json"
            },
            params={
                "startAt": start_at,
                "maxResults": 50
            },
            json={
                "jql": jql,
                "fields": ["summary", "status", "priority", "created", "description"]
            },
            auth=(EMAIL, API_TOKEN)
        )

        print("Status:", response.status_code)
        print("Response:", response.text[:300])

        data = response.json()
        batch = data.get("issues", [])

        print("Fetched:", len(batch))
        issues.extend(batch)

        if len(batch) < 50:
            break

        start_at += 50

    return issues

# ==============================
# 🧠 EXTRACT TEXT FROM ADF
# ==============================

def extract_text_from_adf(desc):
    text = ""

    def extract(node):
        nonlocal text

        if isinstance(node, dict):
            if "text" in node:
                text += node["text"] + " "
            if "content" in node:
                for child in node["content"]:
                    extract(child)

        elif isinstance(node, list):
            for item in node:
                extract(item)

    extract(desc)
    return text

# ==============================
# 🧠 PARSE DESCRIPTION
# ==============================

def extract_from_description(desc):
    text = extract_text_from_adf(desc)
    text = re.sub(r'\s+', ' ', text)

    def find(pattern):
        match = re.search(pattern, text, re.IGNORECASE)
        return match.group(1).strip() if match else ""

    cve_id = find(r"CVE ID:\s*(CVE-\d{4}-\d+)")
    severity = find(r"CVSS Severity:\s*([A-Z]+)")
    package = find(r"Package:\s*(.*?)(?:Location:|Framework:|Fix available:)")
    image_version = find(r"Image details:.*?Version:\s*([\w\.]+)")

    fix_match = re.search(r"Recommended:\s*version\s*([\d\.]+)", text, re.IGNORECASE)
    fix_available = f"Upgrade to {fix_match.group(1)}" if fix_match else "No"

    note = find(r"Upwind CVE name:\s*([^\.]+)")

    return cve_id, severity, package.strip(), image_version, fix_available, note.strip()

# ==============================
# 🧠 CLUSTER + ENV
# ==============================

def extract_cluster_env(desc):
    text = extract_text_from_adf(desc)

    match = re.search(r"Path:\s*(.+)", text)
    if not match:
        return "Unknown", "Unknown"

    path = match.group(1).lower()

    if "superapi" in path:
        cluster = "SuperAPI"
    elif "askmepay" in path:
        cluster = "AskMePay"
    elif "artemis" in path:
        cluster = "Artemis"
    else:
        cluster = "Unknown"

    if "prod" in path:
        env = "PROD"
    elif "sdlc" in path or "dev" in path:
        env = "DEV"
    else:
        env = "Unknown"

    return cluster, env

# ==============================
# 🔄 PROCESS ISSUE
# ==============================

def process_issue(issue):
    fields = issue["fields"]

    summary = fields.get("summary", "")
    description = fields.get("description", "")

    ticket_no = issue["key"]
    ticket_link = f"{JIRA_URL}/browse/{ticket_no}"

    created = fields.get("created", "")
    date = created.split("T")[0]
    month = datetime.strptime(date, "%Y-%m-%d").strftime("%B")

    status = fields.get("status", {}).get("name", "")
    fallback_severity = fields.get("priority", {}).get("name", "")

    cluster, env = extract_cluster_env(description)

    cve_id, severity_desc, package, image_version, fix, note = extract_from_description(description)

    severity = severity_desc if severity_desc else fallback_severity

    row = [
        ticket_no,
        summary,
        cve_id,
        severity,
        package,
        image_version,
        fix,
        ticket_link,
        date,
        status,
        note,
        image_version,
        "",
        "",
        month,
        cluster,
        env
    ]

    return row, month, cluster

# ==============================
# 📊 GOOGLE SHEETS
# ==============================

def connect_sheets():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))

    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)

    return client.open(GOOGLE_SHEET_NAME)

def get_or_create_sheet(client, sheet_name):
    try:
        return client.worksheet(sheet_name)
    except:
        sheet = client.add_worksheet(title=sheet_name, rows="1000", cols="20")
        sheet.append_row(COLUMNS)
        format_sheet(sheet)
        return sheet

# ==============================
# 🔄 SYNC
# ==============================

def sync_all(client):
    issues = fetch_jira_issues(JQL)

    print("Total issues:", len(issues))

    sheet_cache = {}

    for issue in issues:
        result = process_issue(issue)

        if not result:
            continue

        row, month, cluster = result

        sheet_name = f"{cluster}-{month}"
        print(f"➡ Writing {row[0]} → {sheet_name}")

        if sheet_name not in sheet_cache:
            sheet_cache[sheet_name] = get_or_create_sheet(client, sheet_name)

        sheet = sheet_cache[sheet_name]

        existing = sheet.get_all_records()
        index = {r["Ticket No"]: i + 2 for i, r in enumerate(existing)}

        ticket_no = row[0]
        severity = row[3]

        if ticket_no in index:
            row_index = index[ticket_no]

            sheet.update(
                values=[row],
                range_name=f"A{row_index}:Q{row_index}"
            )
        else:
            sheet.append_row(row)
            row_index = len(existing) + 2

        if severity == "CRITICAL":
            color = {"red": 1, "green": 0.8, "blue": 0.8}
        elif severity == "HIGH":
            color = {"red": 1, "green": 0.9, "blue": 0.7}
        elif severity == "MEDIUM":
            color = {"red": 1, "green": 1, "blue": 0.8}
        else:
            color = None

        if color:
            sheet.format(
                f"A{row_index}:Q{row_index}",
                {"backgroundColor": color}
            )

# ==============================
# 🎨 FORMAT
# ==============================

def format_sheet(sheet):
    sheet.format("A1:Q1", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 1}
    })

    sheet.freeze(rows=1)
    sheet.set_basic_filter("A1:Q1000")

# ==============================
# 🚀 MAIN
# ==============================

def main():
    try:
        print("🔄 Running at", datetime.now())
        client = connect_sheets()
        sync_all(client)
        print("✅ Done!")
    except Exception as e:
        print("❌ ERROR:", e)

# ==============================
# ▶ RUN
# ==============================

if __name__ == "__main__":
    main()
