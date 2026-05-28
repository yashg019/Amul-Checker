"""
Amul Protein Stock Checker
Checks shop.amul.com and sends push notifications (ntfy) + email when stock changes.
"""

import requests
import json
import os
import sys
import hashlib
import random
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ============================================================
# CONFIG — edit these OR set the matching environment variable
#          (env vars take priority, so GitHub Secrets override these)
# ============================================================
PINCODE    = "500032"
PRODUCTS_TO_TRACK = [
    "High Protein Rose Lassi",
]

# ntfy push notification (set "" to disable)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "amul-protein-yash-9271")

# Gmail — local use: fill in below. GitHub: leave as-is, set Secrets instead.
EMAIL_FROM     = os.environ.get("EMAIL_FROM",     "you@gmail.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO",       "you@gmail.com")
# ============================================================

STORE_ID   = "62fa94df8c13af2e242eba16"
BASE_URL   = "https://shop.amul.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "amul_stock_state.json")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------------
# Session + auth
# ---------------------------------------------------------------------------

def _create_session():
    """
    Return (requests.Session, server_ts, session_tid).
    Visits the homepage to collect cookies and the server timestamp,
    then reads /user/info.js for the session tid used in SHA-256 auth.
    """
    s = requests.Session()
    s.headers.update({"user-agent": _UA})

    r = s.get(BASE_URL + "/", timeout=15)
    r.raise_for_status()

    m = re.search(r'serverTimestamp\s*=\s*"(\d+)"', r.text)
    if not m:
        raise RuntimeError("Could not find serverTimestamp on Amul homepage.")
    server_ts = m.group(1)

    r2 = s.get(BASE_URL + "/user/info.js", timeout=15)
    r2.raise_for_status()
    content = r2.content.decode("utf-8", errors="replace")
    m2 = re.search(r'session\s*=\s*(\{.+\})', content)
    if not m2:
        raise RuntimeError("Could not parse session from /user/info.js.")
    sess = json.loads(m2.group(1))
    session_tid = sess["tid"]

    return s, server_ts, session_tid


def _make_tid(server_ts, session_tid):
    """Compute the SHA-256 challenge token the Amul frontend uses for every API call."""
    rand = random.randint(0, 999)
    preimage = f"{STORE_ID}:{server_ts}:{rand}:{session_tid}"
    digest = hashlib.sha256(preimage.encode()).hexdigest()
    return f"{server_ts}:{rand}:{digest}"


def _hdrs(server_ts, session_tid, referer=BASE_URL + "/"):
    """Return request headers with a fresh auth tid."""
    return {
        "accept":       "application/json",
        "referer":      referer,
        "content-type": "application/json",
        "tid":          _make_tid(server_ts, session_tid),
        "frontend":     "1",
        "base_url":     referer,
    }


# ---------------------------------------------------------------------------
# Pincode → substore
# ---------------------------------------------------------------------------

def _resolve_substore(s, pincode, server_ts, session_tid):
    """
    Auto-detect the Amul substore alias for a given pincode.
    Strategy: look up state from ms.india_pincodes, then match against ms.substores by name.
    """
    # 1. Get state name for pincode
    r = s.get(
        f"{BASE_URL}/api/1/entity/ms.india_pincodes",
        params={"q[pin_code]": pincode, "limit": 1, "fields": "pin_code,state_name"},
        headers=_hdrs(server_ts, session_tid),
        timeout=15,
    )
    r.raise_for_status()
    items = r.json().get("data", [])
    if not items:
        raise ValueError(f"Pincode {pincode} not found in Amul's database.")
    state = items[0]["state_name"].lower()   # e.g. "telangana"
    print(f"  Pincode {pincode} -> state: {items[0]['state_name']}")

    # 2. List all substores and match by alias or name
    r2 = s.get(
        f"{BASE_URL}/api/1/entity/ms.substores",
        params={"limit": 100, "fields": "alias,name"},
        headers=_hdrs(server_ts, session_tid),
        timeout=15,
    )
    r2.raise_for_status()
    substores = r2.json().get("data", [])

    # Exact alias match first, then name prefix match
    for sub in substores:
        if sub.get("alias", "").lower() == state:
            print(f"  Matched substore: {sub['name']} (alias: {sub['alias']})")
            return sub["alias"]
    for sub in substores:
        if state.startswith(sub.get("alias", "").lower()):
            print(f"  Matched substore (prefix): {sub['name']} (alias: {sub['alias']})")
            return sub["alias"]
    for sub in substores:
        if sub.get("name", "").lower() in state or state in sub.get("name", "").lower():
            print(f"  Matched substore (name): {sub['name']} (alias: {sub['alias']})")
            return sub["alias"]

    available = [s.get("alias") for s in substores]
    raise ValueError(
        f"Could not match state '{state}' to any Amul substore.\n"
        f"Available substores: {available}\n"
        f"Set SUBSTORE manually at the top of this script."
    )


def _set_substore(s, substore_alias, server_ts, session_tid):
    """PUT setPreferences to lock the session to the given substore."""
    r = s.put(
        f"{BASE_URL}/api/1/entity/ms.settings/_/setPreferences",
        data=json.dumps({"data": {"store": substore_alias}}),
        headers=_hdrs(server_ts, session_tid),
        timeout=15,
    )
    r.raise_for_status()
    print(f"  Substore set to: {substore_alias}")


# ---------------------------------------------------------------------------
# Product fetching
# ---------------------------------------------------------------------------

def _fetch_protein_products(s, server_ts, session_tid):
    """Fetch all products in the 'protein' category for the active substore."""
    all_products = []
    limit, skip = 100, 0
    while True:
        r = s.get(
            f"{BASE_URL}/api/1/entity/ms.products",
            params={
                "q[categories]": "protein",
                "limit": limit,
                "skip":  skip,
            },
            headers=_hdrs(server_ts, session_tid),
            timeout=15,
        )
        r.raise_for_status()
        batch = r.json().get("data", [])
        all_products.extend(batch)
        if len(batch) < limit:
            break
        skip += limit
    return all_products


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _send_ntfy(title, message, priority="default", tags="shopping_cart"):
    if not NTFY_TOPIC:
        return
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10,
        )
        r.raise_for_status()
        print(f"  [ntfy]  Sent: {title}")
    except Exception as e:
        print(f"  [ntfy]  Failed: {e}")


def _send_email(subject, body):
    if not EMAIL_FROM:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(EMAIL_FROM, EMAIL_PASSWORD.replace(" ", ""))
            smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_bytes())
        print(f"  [email] Sent: {subject}")
    except Exception as e:
        print(f"  [email] Failed: {e}")


def _notify(title, body, ntfy_priority="default", ntfy_tags="shopping_cart"):
    """Fire both ntfy push and email notification."""
    _send_ntfy(title, body, priority=ntfy_priority, tags=ntfy_tags)
    _send_email(subject=f"[Amul] {title}", body=body)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def check_stock():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{now}] Amul stock check — pincode {PINCODE}")

    # 1. Establish session + auth
    try:
        sess, server_ts, session_tid = _create_session()
    except Exception as e:
        print(f"ERROR setting up session: {e}")
        sys.exit(1)

    # 2. Resolve pincode → substore
    try:
        substore = _resolve_substore(sess, PINCODE, server_ts, session_tid)
    except Exception as e:
        print(f"ERROR resolving substore: {e}")
        sys.exit(1)

    # 3. Activate substore in session
    try:
        _set_substore(sess, substore, server_ts, session_tid)
    except Exception as e:
        print(f"ERROR setting substore: {e}")
        sys.exit(1)

    # 4. Fetch protein products
    try:
        products = _fetch_protein_products(sess, server_ts, session_tid)
        print(f"  Protein products in catalogue: {len(products)}")
    except Exception as e:
        print(f"ERROR fetching products: {e}")
        sys.exit(1)

    # 5. Filter to tracked keywords
    tracked = [p for p in products
               if any(kw.lower() in (p.get("name") or "").lower()
                      for kw in PRODUCTS_TO_TRACK)]

    if not tracked:
        print()
        print("  WARNING: None of your tracked products were found.")
        print(f"  Keywords : {PRODUCTS_TO_TRACK}")
        print("  All protein products in the catalogue:")
        for p in products:
            avail = "[IN STOCK]" if p.get("available") else "[OUT OF STOCK]"
            print(f"    {avail} {p.get('name')}")
        print()
        print("  Copy a product name (or a unique substring) from the list above")
        print("  into PRODUCTS_TO_TRACK at the top of this script.")
        return

    # 6. Compare against saved state and notify on changes
    state     = _load_state()
    new_state = {}

    for p in tracked:
        name      = p.get("name", "Unknown")
        alias     = p.get("alias") or name
        available = bool(p.get("available"))

        new_state[alias] = {"name": name, "available": available}
        prev = state.get(alias)

        if prev is None:
            label = "IN STOCK" if available else "OUT OF STOCK"
            print(f"  [NEW]     {name} - {label}")
            _notify(
                title       = f"Amul Tracker: {name}",
                body        = f"Now tracking '{name}'. Current status: {label}.",
                ntfy_priority = "low",
                ntfy_tags   = "white_check_mark",
            )
        elif prev["available"] != available:
            if available:
                print(f"  [RESTOCK] {name} is BACK IN STOCK!")
                _notify(
                    title       = f"RESTOCK: {name}",
                    body        = f"'{name}' is back in stock on Amul Shop! Order now.",
                    ntfy_priority = "urgent",
                    ntfy_tags   = "package,tada",
                )
            else:
                print(f"  [OOS]     {name} went OUT OF STOCK")
                _notify(
                    title       = f"Out of Stock: {name}",
                    body        = f"'{name}' is now out of stock on Amul Shop.",
                    ntfy_priority = "default",
                    ntfy_tags   = "x",
                )
        else:
            label = "in stock" if available else "out of stock"
            print(f"  [OK]      {name} - {label} (no change)")

    _save_state(new_state)
    print("Done.\n")


if __name__ == "__main__":
    check_stock()
