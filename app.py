"""
TicketMamba Webhook Server
- Receives real-time sale notifications from Automatiq
- Sends WhatsApp confirmation via Twilio
- On Y reply: updates the Google Sheet and highlights the row yellow
- On N reply: skips

Deploy to Railway. Set env vars before deploying (see README below).
"""

import json
import os
import re
import warnings
from collections import OrderedDict
from datetime import datetime

import requests
from flask import Flask, request
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
import gspread
from google.oauth2.service_account import Credentials

warnings.filterwarnings("ignore")

app = Flask(__name__)

# ─── Config (all from environment variables) ──────────────────────────────────

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM        = "whatsapp:+14155238886"          # Twilio sandbox number
USER_WHATSAPP      = "whatsapp:+16476218874"          # your number

AUTOMATIQ_API_KEY  = os.environ["AUTOMATIQ_API_KEY"]

SPREADSHEET_IDS = {
    "2025-2026": "1nANBLCplrjKqjAjDSMjdQ1mdHCHIMQud-OfYOO5gJ7c",
    "2026-2027": "1sdW8ILf4VcLjg0wmJwdCZ8kRyqbrwyMq8bL0uf_adCA",
}

SITE_TYPE_MAP = {
    "AM": "TM Resale",
    None: "Automatiq",
}

SPECIAL_EVENT_KEYWORDS = [
    "ufc", "wwe", "wrestling", "monday night raw", "smackdown",
    "boxing", "fight night", "comedy", "chappelle",
]

CANADIAN_COUNTRIES = {"CA", "Canada"}

SKIP_TABS = {
    "Landing Page", "Summary", "PSL Database", "2025 PSL Purchases",
    "2026 PSL Purchases", "PSL Sales", "TEST- Credit Card Forecast",
    "Systems Logins", "AMEX Cards", "Dropdown Lists", "Scotiabank Tunnel Club",
}

# ─── In-memory queue: OrderedDict preserves insertion order (oldest first) ────
# Each entry: sale_id → { message, tab, season, row_idx, qty_sold, price_cad, source }
pending_sales: OrderedDict = OrderedDict()


# ─── Google Sheets ────────────────────────────────────────────────────────────

def connect_sheets():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    client = gspread.authorize(creds)
    return {season: client.open_by_key(sid) for season, sid in SPREADSHEET_IDS.items()}



# ─── Tab routing ──────────────────────────────────────────────────────────────

def find_tab(event_name, venue_name, event_type, available_tabs):
    name_lower  = event_name.lower()
    venue_lower = venue_name.lower()
    for tab in available_tabs:
        if tab.lower() in name_lower:
            return tab
    if event_type == "CONCERT":
        return "Budweiser Stage" if "budweiser" in venue_lower else "Concerts"
    if event_type in ("THEATER", "SPORT"):
        return "Special Events"
    if any(kw in name_lower for kw in SPECIAL_EVENT_KEYWORDS):
        return "Special Events"
    return "Budweiser Stage" if "budweiser" in venue_lower else "Concerts"


# ─── Row matching ─────────────────────────────────────────────────────────────

def parse_seat_range(seats_str):
    nums = re.findall(r"\d+", seats_str)
    if len(nums) >= 2:
        return int(nums[0]), int(nums[-1])
    if len(nums) == 1:
        return int(nums[0]), int(nums[0])
    return None


def seats_match(sheet_seats_str, sale_seats):
    a = parse_seat_range(sheet_seats_str)
    b = parse_seat_range(sale_seats)
    if a is None or b is None:
        return False
    return a[0] <= b[0] and b[1] <= a[1]


def format_date(occurs_at):
    dt = datetime.fromisoformat(occurs_at.replace("Z", "+00:00"))
    return f"{dt.day}-{dt.strftime('%b')}-{dt.strftime('%y')}"


def find_row(rows, event_date, section, row, seats):
    section_norm = section.strip().lower()
    row_norm     = row.strip().lower()
    date_lower   = event_date.lower()
    for i, cells in enumerate(rows):
        col_a = (cells[0] if len(cells) > 0 else "").strip().lower()
        col_c = (cells[2] if len(cells) > 2 else "").strip().lower()
        if not col_a or not col_c:
            continue
        if col_a != date_lower:
            continue
        sec_match = (
            f"section {section_norm}" in col_c
            or re.search(rf"\b{re.escape(section_norm)}\b", col_c) is not None
        )
        if not sec_match:
            continue
        row_match = (
            f"row {row_norm} " in col_c
            or col_c.endswith(f"row {row_norm}")
            or re.search(rf"\brow {re.escape(row_norm)}\b", col_c) is not None
        )
        if not row_match:
            continue
        if seats_match(col_c, seats):
            return i
        return i
    return None


# ─── Currency ─────────────────────────────────────────────────────────────────

def get_usd_to_cad():
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=USD&to=CAD", timeout=5)
        return r.json()["rates"]["CAD"]
    except Exception:
        return 1.38


def to_cad(amount_cents, is_canadian, is_ticketmaster, rate):
    dollars = amount_cents / 100
    if is_canadian and not is_ticketmaster:
        converted = dollars * rate
    else:
        converted = dollars
    return round(round(converted / 5) * 5, 2)


# ─── Sheet update ─────────────────────────────────────────────────────────────

def parse_money(s):
    try:
        return float(re.sub(r"[^\d.]", "", s))
    except (ValueError, TypeError):
        return 0.0


def update_row(sheet, row_index, qty_sold, sale_price_cad, source):
    sheet_row = row_index + 1
    current   = sheet.row_values(sheet_row)

    col_f = current[5] if len(current) > 5 else ""
    col_h = current[7] if len(current) > 7 else ""
    col_i = current[8] if len(current) > 8 else ""
    col_j = current[9] if len(current) > 9 else ""

    original_qty  = int(re.sub(r"[^\d]", "", col_f) or 0)
    current_left  = int(re.sub(r"[^\d]", "", col_h) or original_qty)
    current_price = parse_money(col_i)
    current_src   = col_j.strip()

    new_left  = max(0, current_left - qty_sold)
    new_price = round(current_price + sale_price_cad, 2)
    new_src   = f"{current_src}, {source}".lstrip(", ") if current_src else source

    sheet.update(
        f"H{sheet_row}:J{sheet_row}",
        [[new_left, f"${new_price:,.2f}", new_src]],
        value_input_option="USER_ENTERED",
    )
    sheet.format(f"H{sheet_row}:J{sheet_row}", {
        "backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.4}
    })
    if new_left == 0:
        col_d = current[3] if len(current) > 3 else ""
        if col_d.strip().lower() != "sold out":
            sheet.update_cell(sheet_row, 4, "Sold Out")

    return new_left, new_price, new_src


# ─── Parse Automatiq sale payload ────────────────────────────────────────────

def fetch_sale_by_id(sale_id):
    url = f"https://b2b.automatiq.com/api/ecomm/sales/{sale_id}"
    r = requests.get(url, headers={
        "accept": "application/json",
        "Authorization": AUTOMATIQ_API_KEY,
    })
    return r.json() if r.ok else None


def extract_sale_id(payload):
    """Try multiple locations in the webhook payload to find a sale ID."""
    if not payload:
        return None
    # Direct sale ID
    for key in ("id", "sale_id", "object_id"):
        if key in payload and str(payload[key]).startswith("SA_"):
            return payload[key]
    # Nested under data
    data = payload.get("data", {})
    if isinstance(data, dict):
        sid = data.get("id", "")
        if str(sid).startswith("SA_"):
            return sid
    # Nested under fulfillment → sale relationship
    for key in ("sale_id", "sale", "related_id"):
        val = payload.get(key) or (data.get(key) if isinstance(data, dict) else None)
        if val and str(val).startswith("SA_"):
            return val
    return None


def process_sale_data(sale_response):
    """
    Parse a full sale API response and return a dict with everything
    needed to update the sheet and send a WhatsApp message.
    Returns None if the sale can't be matched.
    """
    data     = sale_response.get("data", {})
    included = {item["id"]: item for item in sale_response.get("included", [])}

    sale_id = data.get("id")
    attrs   = data.get("attributes", {})
    rels    = data.get("relationships", {})

    # Event
    event_id    = rels.get("event", {}).get("data", {}).get("id")
    event_obj   = included.get(event_id, {})
    event_attrs = event_obj.get("attributes", {})
    event_name  = event_attrs.get("event_name", "")
    occurs_at   = event_attrs.get("occurs_at", "")

    # Venue
    venue_id    = (event_obj.get("relationships", {})
                            .get("venue", {}).get("data", {}).get("id"))
    venue_obj   = included.get(venue_id, {}) if venue_id else {}
    venue_attrs = venue_obj.get("attributes", {})
    venue_name  = venue_attrs.get("name", "")
    venue_country = venue_attrs.get("country", "")

    # Category
    performer_id = (event_obj.get("relationships", {})
                             .get("performer", {}).get("data", {}).get("id"))
    performer_obj = included.get(performer_id, {}) if performer_id else {}
    cat_id = (performer_obj.get("relationships", {})
                           .get("category", {}).get("data", {}).get("id"))
    cat_obj    = included.get(cat_id, {}) if cat_id else {}
    event_type = cat_obj.get("attributes", {}).get("event_type", "")

    # Fulfillment → site_type
    site_type = None
    for fref in rels.get("fulfillments", {}).get("data", []):
        f = included.get(fref["id"], {})
        st = f.get("attributes", {}).get("site_type")
        if st is not None:
            site_type = st
            break

    # Sale fields
    section  = attrs.get("section", "")
    row      = attrs.get("row", "")
    seats    = attrs.get("seats", "")
    qty_sold = attrs.get("tickets_quantity", 0)
    subtotal = attrs.get("order_sub_total", 0)

    if not occurs_at or not section or not row:
        return None

    event_date = format_date(occurs_at)
    rate       = get_usd_to_cad()
    is_canadian    = venue_country in CANADIAN_COUNTRIES
    is_ticketmaster = site_type == "AM"
    price_cad = to_cad(subtotal, is_canadian, is_ticketmaster, rate)

    source_name = SITE_TYPE_MAP.get(site_type, str(site_type))
    source      = f"{source_name} ({qty_sold})"

    return {
        "sale_id":    sale_id,
        "event_name": event_name,
        "event_date": event_date,
        "venue_name": venue_name,
        "event_type": event_type,
        "section":    section,
        "row":        row,
        "seats":      seats,
        "qty_sold":   qty_sold,
        "price_cad":  price_cad,
        "source":     source,
    }


def find_sheet_location(sale_info):
    """
    Search both spreadsheets for the matching row.
    Only loads the one target tab per season (not all tabs) to avoid timeouts.
    Returns (season, tab, row_idx, worksheet) or None.
    """
    spreadsheets = connect_sheets()

    for season, spreadsheet in spreadsheets.items():
        try:
            all_ws = spreadsheet.worksheets()
        except Exception as e:
            print(f"  WARNING: could not list worksheets for {season}: {e}")
            continue

        available_tabs = [ws.title for ws in all_ws]
        tab = find_tab(
            sale_info["event_name"], sale_info["venue_name"],
            sale_info["event_type"], available_tabs,
        )
        if not tab:
            continue

        try:
            ws = spreadsheet.worksheet(tab)
            rows = ws.get_all_values()
        except Exception as e:
            print(f"  WARNING: could not load [{season}|{tab}]: {e}")
            continue

        row_idx = find_row(
            rows,
            sale_info["event_date"],
            sale_info["section"],
            sale_info["row"],
            sale_info["seats"],
        )
        if row_idx is not None:
            return season, tab, row_idx, ws

    return None


# ─── WhatsApp ─────────────────────────────────────────────────────────────────

def send_whatsapp(msg):
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(body=msg, from_=TWILIO_FROM, to=USER_WHATSAPP)


def build_whatsapp_message(sale_info, season, tab, row_idx):
    return (
        f"🎟 *New Automatiq Sale*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*{sale_info['event_name']}*\n"
        f"📅 {sale_info['event_date']}\n"
        f"💺 Sec {sale_info['section']} | Row {sale_info['row']} | Seats {sale_info['seats']}\n"
        f"🎫 Qty sold: {sale_info['qty_sold']}\n"
        f"💰 ${sale_info['price_cad']:,.2f} CAD\n"
        f"🏪 {sale_info['source']}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📊 Sheet: {season} › {tab} (sheet row {row_idx + 1})\n\n"
        f"Reply *Y* to log it or *N* to skip."
    )


# ─── Automatiq webhook endpoint ───────────────────────────────────────────────

@app.route("/webhook/automatiq", methods=["POST"])
def automatiq_webhook():
    try:
        payload = request.json or {}
        print(f"[Automatiq] Received webhook: {json.dumps(payload)[:300]}")

        sale_id = extract_sale_id(payload)
        if not sale_id:
            print("[Automatiq] Could not extract sale ID from payload — logging raw payload.")
            return "", 200

        if sale_id in pending_sales:
            print(f"[Automatiq] Sale {sale_id} already pending, ignoring duplicate.")
            return "", 200

        # Fetch full sale details
        sale_response = fetch_sale_by_id(sale_id)
        if not sale_response:
            print(f"[Automatiq] Could not fetch sale {sale_id}")
            return "", 200

        sale_info = process_sale_data(sale_response)
        if not sale_info:
            print(f"[Automatiq] Could not parse sale {sale_id}")
            return "", 200

        print(f"[Automatiq] Parsed: {sale_info['event_name']} | {sale_info['event_date']} | Sec {sale_info['section']} Row {sale_info['row']}")

        # Find matching sheet location
        location = find_sheet_location(sale_info)
        if not location:
            print(f"[Automatiq] No sheet row found for sale {sale_id} — skipping WhatsApp.")
            return "", 200

        season, tab, row_idx, worksheet = location

        # Build and send WhatsApp
        msg = build_whatsapp_message(sale_info, season, tab, row_idx)

        # Only send WhatsApp if no other sale is pending (queue them)
        was_empty = len(pending_sales) == 0
        pending_sales[sale_id] = {
            "sale_info":  sale_info,
            "season":     season,
            "tab":        tab,
            "row_idx":    row_idx,
            "message":    msg,
        }

        if was_empty:
            send_whatsapp(msg)
            print(f"[Automatiq] WhatsApp sent for {sale_id} ({tab} row {row_idx + 1})")
        else:
            print(f"[Automatiq] Sale {sale_id} queued ({len(pending_sales)} pending)")

        return "", 200

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[Automatiq] UNHANDLED ERROR: {e}\n{tb}")
        return {"error": str(e), "traceback": tb}, 500


# ─── Twilio reply endpoint ────────────────────────────────────────────────────

@app.route("/webhook/twilio", methods=["POST"])
def twilio_reply():
    body = request.form.get("Body", "").strip().upper()
    resp = MessagingResponse()

    if not pending_sales:
        resp.message("No pending sales to confirm.")
        return str(resp)

    # Process oldest pending sale
    sale_id, entry = next(iter(pending_sales.items()))
    sale_info  = entry["sale_info"]
    tab        = entry["tab"]
    row_idx    = entry["row_idx"]

    if body in ("Y", "YES"):
        # Re-connect to sheets and update the row
        try:
            spreadsheets = connect_sheets()
            worksheet    = spreadsheets[entry["season"]].worksheet(tab)
            new_left, new_price, new_src = update_row(
                worksheet,
                row_idx,
                sale_info["qty_sold"],
                sale_info["price_cad"],
                sale_info["source"],
            )
            del pending_sales[sale_id]
            resp.message(
                f"✅ *Logged!*\n"
                f"{tab} | Sheet row {row_idx + 1}\n"
                f"Qty left: {new_left} | ${new_price:,.2f} CAD\n"
                f"Source: {new_src}"
            )
            print(f"[Twilio] Confirmed {sale_id} — sheet updated.")
        except Exception as e:
            resp.message(f"❌ Error updating sheet: {e}")
            print(f"[Twilio] Error on {sale_id}: {e}")

    elif body in ("N", "NO"):
        del pending_sales[sale_id]
        resp.message(f"⏭ Skipped {sale_info['event_name']} ({sale_info['event_date']})")
        print(f"[Twilio] Skipped {sale_id}")

    else:
        resp.message("Reply *Y* to confirm or *N* to skip.")
        return str(resp)

    # Send next queued sale if any
    if pending_sales:
        next_id, next_entry = next(iter(pending_sales.items()))
        send_whatsapp(next_entry["message"])
        print(f"[Twilio] Sent next queued sale: {next_id}")

    return str(resp)


# ─── Health check ─────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "pending": len(pending_sales)}, 200


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
