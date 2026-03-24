
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

JQL = "project = MDRS AND created >= -7d ORDER BY created DESC"


# ==============================
# 📊 WORKFLOW ORDER (IMPORTANT)
# ==============================

WORKFLOW = [
    "Backlog",
    "Selected for Development",
    "In Progress",
    "Done"
]

# ==============================
# 📊 COLUMNS INDEX (0-based)
# ==============================

COL_STATUS = 9      # J
COL_JIRA_UPDATE = 12  # M
COL_APPROVAL = 17   # R

# ==============================
# 📥 FETCH JIRA DATA
# ==============================

def fetch_jira_issues():
    issues = []
    start_at = 0

    while True:
        response = requests.post(
            f"{JIRA_URL}/rest/api/3/search/jql",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json"
            },
            params={"startAt": start_at, "maxResults": 50},
            json={
                "jql": JQL,
                "fields": ["summary", "status", "priority", "created", "description"]
            },
            auth=(EMAIL, API_TOKEN)
        )

        data = response.json()
        batch = data.get("issues", [])
        print("Fetched:", len(batch))

        issues.extend(batch)

        if len(batch) < 50:
            break

        start_at += 50

    return issues

# ==============================
# 🧠 GET CURRENT STATUS
# ==============================

def get_current_status(issue_key):
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}"
    res = requests.get(url, auth=(EMAIL, API_TOKEN))
    data = res.json()
    return data["fields"]["status"]["name"]

# ==============================
# 🧠 GET TRANSITIONS
# ==============================

def get_transitions(issue_key):
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/transitions"

    res = requests.get(
        url,
        auth=(EMAIL, API_TOKEN),
        headers={"Accept": "application/json"}
    )

    return res.json().get("transitions", [])

# ==============================
# 🧠 APPLY TRANSITION
# ==============================

def apply_transition(issue_key, transition_id):
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/transitions"

    payload = {"transition": {"id": transition_id}}

    res = requests.post(
        url,
        json=payload,
        auth=(EMAIL, API_TOKEN),
        headers={"Content-Type": "application/json"}
    )

    print(f"{issue_key} → {res.status_code}")

# ==============================
# 🧠 MOVE ISSUE (AUTO STEP)
# ==============================

def move_issue(issue_key, target_status):
    current_status = get_current_status(issue_key)

    print(f"{issue_key}: {current_status} → {target_status}")

    if current_status == target_status:
        return True

    try:
        current_index = WORKFLOW.index(current_status)
        target_index = WORKFLOW.index(target_status)
    except ValueError:
        print("Workflow mismatch")
        return False

    step_range = range(current_index + 1, target_index + 1)

    for i in step_range:
        next_status = WORKFLOW[i]
        transitions = get_transitions(issue_key)

        transition_id = None
        for t in transitions:
            if t["name"].lower() == next_status.lower():
                transition_id = t["id"]
                break

        if not transition_id:
            print(f"No transition to {next_status}")
            return False

        apply_transition(issue_key, transition_id)

    return True

# ==============================
# 📊 GOOGLE SHEETS
# ==============================

def connect_sheets():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
    creds_dict, scope
)

    client = gspread.authorize(creds)

    return client.open(GOOGLE_SHEET_NAME)

# ==============================
# 🔄 SYNC LOGIC (CORE)
# ==============================

def sync_sheet_to_jira(sheet):
    data = sheet.get_all_values()

    headers = data[0]
    rows = data[1:]

    # =========================
    # 🔒 COLUMN AUTO SYNC
    # =========================
    REQUIRED_COLUMNS = [
        "Ticket No", "CVE Names", "CVE ID", "Severity", "Package",
        "Image Version", "Fix Available", "Ticket Link", "Date",
        "Status", "Note", "Image Current Version",
        "Jira Update ticket", "Timeline", "Month",
        "Cluster", "Environment", "Approval"
    ]

    if len(headers) < len(REQUIRED_COLUMNS):
        print("Fixing missing columns...")
        sheet.update("A1", [REQUIRED_COLUMNS])
        headers = REQUIRED_COLUMNS
def apply_dropdown(sheet):
    sheet.spreadsheet.batch_update({
        "requests": [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": sheet.id,
                        "startRowIndex": 1,
                        "endRowIndex": 1000,
                        "startColumnIndex": 17,
                        "endColumnIndex": 18
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [
                                {"userEnteredValue": "Yes"},
                                {"userEnteredValue": "No"}
                            ]
                        },
                        "showCustomUi": True
                    }
                }
            }
        ]
    })
    # =========================
    # 🔒 BULK LIMIT
    # =========================
    MAX_BULK = 10
    processed = 0

    for i, row in enumerate(rows, start=2):

        # 🔒 Prevent short row crash
        ticket = row[0] if len(row) > 0 else ""
        status = row[9] if len(row) > 9 else ""
        jira_flag = row[12] if len(row) > 12 else ""
        approval = row[17] if len(row) > 17 else ""

        if not ticket:
            continue

        print(f"{ticket} | Status={status} | Approval={approval} | Flag={jira_flag}")

        # =========================
        # 🔒 APPROVAL CHECK
        # =========================
        if approval.strip().lower() != "yes":
            continue

        # =========================
        # 🔒 ALREADY PROCESSED
        # =========================
        if jira_flag == "Done":
            continue

        # =========================
        # 🔒 STATUS VALIDATION
        # =========================
        status = status.strip()
        if status not in WORKFLOW:
            print(f"Invalid status: {status}")
            continue
        # =============================
        # 🎯 APPLY DROPDOWN (ADD HERE)
        # =============================
            apply_dropdown(sheet)

        # =========================
        # 🔒 BULK CONTROL
        # =========================
        if processed >= MAX_BULK:
            print("Bulk limit reached. Remaining skipped.")
            break

        print(f"Processing {ticket}...")

        success = move_issue(ticket, status)

        if success:
            # ✅ Mark as done
            sheet.update_cell(i, 13, "Done")  # Column M

            # ✅ Reset approval
            sheet.update_cell(i, 18, "No")    # Column R

            processed += 1

        else:
            print(f"FAILED: {ticket}")
# ==============================
# 🎨 SHEET UI
# ==============================


# ==============================
# 🚀 MAIN
# ==============================

def main():
    try:
        print("Running at:", datetime.now())

        client = connect_sheets()

        for sheet in client.worksheets():
            print(f"Processing sheet: {sheet.title}")
            sync_sheet_to_jira(sheet)

        print("✅ Done!")

    except Exception as e:
        print("❌ ERROR:", e)

# ==============================
# ▶ RUN
# ==============================

if __name__ == "__main__":
    main()
