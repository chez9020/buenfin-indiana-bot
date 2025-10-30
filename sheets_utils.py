# sheets_utils.py
import os, re
import gspread
from google.oauth2.service_account import Credentials

SHEETS_ID  = os.getenv("GOOGLE_SHEETS_ID")
CRED_PATH  = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
SHEETS_TAB = os.getenv("GOOGLE_SHEETS_TAB", "tickets")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def open_worksheet():
    if not CRED_PATH:
        raise ValueError("Falta GOOGLE_SHEETS_CREDENTIALS")
    if not SHEETS_ID:
        raise ValueError("Falta GOOGLE_SHEETS_ID")
    creds  = Credentials.from_service_account_file(CRED_PATH, scopes=SCOPES)
    client = gspread.authorize(creds)
    sh     = client.open_by_key(SHEETS_ID)
    try:
        return sh.worksheet(SHEETS_TAB)
    except gspread.WorksheetNotFound:
        return sh.sheet1

def parse_money(x) -> float:
    if x is None:
        return 0.0
    s = str(x).strip()
    if not s:
        return 0.0
    s = s.replace("MXN", "").replace("$", "").replace(",", "").strip()
    m = re.findall(r"[-]?\d+(?:\.\d+)?", s)
    try:
        return float(m[0]) if m else 0.0
    except:
        return 0.0