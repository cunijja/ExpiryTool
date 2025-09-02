#!/usr/bin/env python3
# Expiry Inventory System — Rebuilt V2.0
# PART 1/6 — paths, folders, DB, logging, date helpers, report writers

import os, sys, sqlite3, datetime, calendar, textwrap, time, hashlib
import curses, curses.ascii

APP_VERSION = "2.0.0"

# ---------- PATHS & FOLDERS ----------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
REPORTS_PICKLISTS_DIR = os.path.join(REPORTS_DIR, "picklists")
REPORTS_LOW_STOCK_DIR = os.path.join(REPORTS_DIR, "low_stock")
REPORTS_EXPIRIES_DIR = os.path.join(REPORTS_DIR, "expiries")

for d in (DATA_DIR, REPORTS_DIR, REPORTS_PICKLISTS_DIR, REPORTS_LOW_STOCK_DIR, REPORTS_EXPIRIES_DIR):
    os.makedirs(d, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "expiry_inventory.db")
# --- DB helper ------------------------------------------------------------
def get_db():
    """Open a SQLite connection to our app DB."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# ---------- SESSION / NAV ----------

CURRENT_USER = {"initials": "", "is_admin": False}
CURRENT_PATH = ["MAIN"]
_MENU_CURSOR = {}  # path -> last index
MENU_MEMORY = _MENU_CURSOR  # back-compat alias for old code paths

def path_to_str(path_list): 
    return "/".join(path_list)

def push_path(seg: str):
    parts = [p for p in str(seg).split('/') if p]
    if not parts:
        return
    for p in parts:
        # don't duplicate the root
        if p == "MAIN" and CURRENT_PATH == ["MAIN"]:
            continue
        # avoid immediate repeats like PPM/PPM or REPORTS/REPORTS
        if CURRENT_PATH and CURRENT_PATH[-1] == p:
            continue
        CURRENT_PATH.append(p)

def pop_path():
    if len(CURRENT_PATH) > 1:
        CURRENT_PATH.pop()

# ---------- DB CORE ----------

# ==== USERS (admin-managed) ================================================

# ---- Audit log screen ------------------------------------------------------
def _render_log_row(i, r, w):
    details = r.get("details") or ""
    if len(details) > 50:
        details = details[:47] + "..."
    who = (r.get("user") or "-")[:6]
    act = (r.get("action") or "")[:14]
    ts  = (r.get("ts") or "")[:19]
    return f"{r['id']:>5}  {ts}  {who:<6}  {act:<14}  {details}"

def dao_logs_list(limit: int = 500):
    """Return latest log rows, tolerant of different 'user' column names."""
    with get_db() as con:
        cols = [r["name"] for r in con.execute("PRAGMA table_info(logs)").fetchall()]
        user_col = ("user" if "user" in cols else
                    "created_by" if "created_by" in cols else
                    "who" if "who" in cols else None)

        if user_col:
            sql = f"""
                SELECT id, ts, {user_col} AS user, action, details
                FROM logs
                ORDER BY id DESC
                LIMIT ?
            """
        else:
            # No user-like column; still return something so UI works
            sql = """
                SELECT id, ts, action, details, '' AS user
                FROM logs
                ORDER BY id DESC
                LIMIT ?
            """

        rows = con.execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]

def screen_audit_log(stdscr):
    push_path("MAIN/ADMIN/LOGS")
    try:
        while True:
            rows = dao_logs_list(500)

            init_theme(stdscr)
            title_bar(stdscr, path_to_str(CURRENT_PATH), "AUDIT LOG (latest 500)")
            help_line = "[Enter] View   [D] Delete   [R] Refresh   [P] Purge All   [F2] Back"
            stdscr.addstr(3, 2, help_line, curses.A_DIM)
            stdscr.refresh()

            # Empty state: show a prompt and wait for a key
            if not rows:
                stdscr.addstr(5, 2, "No entries yet.")
                stdscr.refresh()
                k = stdscr.getch()  # <-- make sure k exists
                if k in (curses.KEY_F2, 27):   # back
                    return
                if k in (ord('r'), ord('R')):  # refresh
                    continue
                if k in (ord('p'), ord('P')):  # purge (no-op on empty)
                    with no_audit():
                        dao_logs_purge()
                    info_toast(stdscr, "Purged.")
                    continue
                continue  # ignore anything else and redraw

            # Show the list and get a selection
            sel = list_paginated(stdscr, "AUDIT LOG", rows, _render_log_row, page_size=14)
            if sel == -1:        # F2/back from the pager
                return

            r = rows[sel]

            # Key loop for actions on the selected row
            while True:
                k = stdscr.getch()
                if k in (curses.KEY_F2, 27):   # back to previous menu
                    return
                elif k in (ord('r'), ord('R')):  # refresh list
                    break  # break to outer while -> redraw and requery
                elif k in (ord('p'), ord('P')):  # purge all
                    if confirm_dialog(stdscr, "Purge log", "Delete all entries?"):
                        with no_audit():
                            dao_logs_purge()
                        info_toast(stdscr, "Purged.")
                    break
                elif k in (ord('d'), ord('D')):  # delete one entry
                    if confirm_dialog(stdscr, "Delete entry", f"Delete log #{r['id']}?"):
                        with no_audit():
                            dao_logs_delete(r["id"])
                        info_toast(stdscr, "Deleted.")
                    break
                elif k in (10, 13):  # Enter -> view details
                    msg = textwrap.fill(r.get("details") or "(no details)", width=70)
                    info_center(
                        stdscr,
                        f"{r.get('action','')} @ {r.get('ts','')} by {r.get('user') or '-'}",
                        msg
                    )
                    # After closing, stay on the same row; keep waiting for a key
                else:
                    # ignore other keys on this screen
                    continue
            # loop continues -> re-fetch rows and redraw

    finally:
        pop_path()


# ---- Admin override (temporary elevation) ----
ADMIN_OVERRIDE = None  # {"id":..., "initials":...}

def admin_gate(stdscr, reason:str="") -> bool:
    """Prompt for admin initials+password if current user isn't admin."""
    global ADMIN_OVERRIDE
    ADMIN_OVERRIDE = None
    if CURRENT_USER.get("is_admin"):
        ADMIN_OVERRIDE = {"id": CURRENT_USER.get("id"), "initials": CURRENT_USER.get("initials")}
        return True

    for _ in range(3):
        f = input_form(
            stdscr,
            "ADMIN OVERRIDE REQUIRED" + (f" – {reason}" if reason else ""),
            [("Admin initials",""), ("Password","")],
            footer="[Enter] OK   [F2] Cancel"
        )
        if f is None:
            return False
        init = (f.get("Admin initials","") or "").strip().upper()
        pw   = f.get("Password","") or ""
        u = dao_user_get_by_initials(init)
        if u and u.get("is_admin") and u.get("pw_hash") == hash_pw(pw):
            ADMIN_OVERRIDE = {"id": u["id"], "initials": u["initials"]}
            info_toast(stdscr, f"Approved by {u['initials']}")
            return True
        warn_toast(stdscr, "Invalid admin credentials.")
    return False

def clear_admin_override():
    global ADMIN_OVERRIDE
    ADMIN_OVERRIDE = None

def log_with_override(action:str, details:str=""):
    who = CURRENT_USER.get("initials","")
    approver = ADMIN_OVERRIDE["initials"] if ADMIN_OVERRIDE else None
    if approver and approver != who:
        details = (details + f" by={who} approved_by={approver}").strip()
    log_action(who, action, details)


def hash_pw(pw: str) -> str:
    return hashlib.sha256((pw or "").encode("utf-8")).hexdigest()

def ensure_users_table():
    with get_db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
              id        INTEGER PRIMARY KEY AUTOINCREMENT,
              initials  TEXT NOT NULL UNIQUE,
              pw_hash   TEXT NOT NULL,
              is_admin  INTEGER NOT NULL DEFAULT 0,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

def dao_user_list():
    with get_db() as con:
        rows = con.execute("SELECT id, initials, is_admin FROM users ORDER BY initials").fetchall()
        return [dict(r) for r in rows]

def dao_user_get(user_id:int):
    with get_db() as con:
        r = con.execute("SELECT id, initials, pw_hash, is_admin FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(r) if r else None

def dao_user_get_by_initials(initials:str):
    with get_db() as con:
        r = con.execute("SELECT id, initials, pw_hash, is_admin FROM users WHERE UPPER(initials)=UPPER(?)",
                        (initials.strip(),)).fetchone()
        return dict(r) if r else None

def dao_user_create(initials:str, password:str, is_admin:bool=False) -> int:
    initials = initials.strip().upper()
    with get_db() as con:
        con.execute("INSERT INTO users (initials, pw_hash, is_admin) VALUES (?,?,?)",
                    (initials, hash_pw(password), 1 if is_admin else 0))
        user_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_action(CURRENT_USER.get("initials",""), "user_create", f"init={initials}, admin={is_admin}")
    return user_id

def dao_user_update(user_id:int, initials:str=None, is_admin:bool=None):
    sets, args = [], []
    if initials is not None:
        sets.append("initials=?"); args.append(initials.strip().upper())
    if is_admin is not None:
        sets.append("is_admin=?");  args.append(1 if is_admin else 0)
    if not sets:
        return
    args.append(user_id)
    with get_db() as con:
        con.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", tuple(args))
    log_action(CURRENT_USER.get("initials",""), "user_update", f"id={user_id}")

def dao_user_set_password(user_id:int, password:str):
    with get_db() as con:
        con.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(password), user_id))
    log_action(CURRENT_USER.get("initials",""), "user_pw_reset", f"id={user_id}")

def dao_user_delete(user_id:int):
    u = dao_user_get(user_id)
    with get_db() as con:
        con.execute("DELETE FROM users WHERE id=?", (user_id,))
    log_action(CURRENT_USER.get("initials",""), "user_delete", f"id={user_id} ({u['initials'] if u else ''})")

# ---- make sure your LOGS table exists (if you don’t already) --------------
def ensure_logs_table():
    with get_db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS logs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT DEFAULT CURRENT_TIMESTAMP,
              user TEXT,
              action TEXT,
              details TEXT
            )
        """)

def dao_logs_delete(log_id:int):
    with get_db() as con:
        con.execute("DELETE FROM logs WHERE id=?", (log_id,))
    log_action(CURRENT_USER.get("initials",""), "log_delete", f"id={log_id}")

def dao_logs_purge():
    with get_db() as con:
        con.execute("DELETE FROM logs")
    log_action(CURRENT_USER.get("initials",""), "log_purge", "")


# helper: get all lots expiring exactly on one day (YYYYMMDD) incl. UPC
def _lots_on_day(ymd: str):
    import sqlite3  # safe if already imported
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT
                i.id              AS item_id,
                IFNULL(i.upc,'')  AS upc,
                i.brand, i.name,
                IFNULL(l.lot,'')  AS lot,
                l.qty,
                l.expiry
            FROM lots l
            JOIN items i ON i.id = l.item_id
            WHERE l.expiry = ?
            ORDER BY i.brand, i.name, IFNULL(l.lot,'')
            """,
            (ymd,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.executescript("""
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS users(
            initials   TEXT PRIMARY KEY,
            password   TEXT NOT NULL,
            is_admin   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS items(
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            brand         TEXT,
            name          TEXT,
            upc           TEXT,
            size          TEXT,
            uom           TEXT,
            vendor        TEXT,
            cost          REAL DEFAULT 0,
            retail        REAL DEFAULT 0,
            notes         TEXT,
            reorder_level INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS lots(
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            lot     TEXT,
            expiry  TEXT,   -- 'YYYYMMDD'
            qty     INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_lots_item   ON lots(item_id);
        CREATE INDEX IF NOT EXISTS idx_lots_expiry ON lots(expiry);

        CREATE TABLE IF NOT EXISTS logs(
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            who     TEXT NOT NULL,
            action  TEXT NOT NULL,
            details TEXT
        );

        -- PICK LISTS
        CREATE TABLE IF NOT EXISTS picklists(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'open', -- open|closed
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL
        );

        -- Items inside a picklist (per-picklist items you asked for)
        CREATE TABLE IF NOT EXISTS picklist_items(
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            picklist_id  INTEGER NOT NULL REFERENCES picklists(id) ON DELETE CASCADE,
            item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            req_qty      INTEGER NOT NULL DEFAULT 0,
            note         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pli_picklist ON picklist_items(picklist_id);
        """)

        # bootstrap admin CB if missing (same as before)
        cur = con.execute("SELECT COUNT(*) c FROM users WHERE initials='CB'")
        if cur.fetchone()["c"] == 0:
            con.execute("INSERT INTO users(initials,password,is_admin) VALUES('CB','CB',1)")
            con.execute("INSERT INTO logs(ts,who,action,details) VALUES(?,?,?,?)",
                        (datetime.datetime.now().isoformat(timespec='seconds'),
                         "SYSTEM", "user_create", "CB admin bootstrap"))
    return True

# ===== DAO compatibility layer (works even if older helpers are missing) =====
def dao_item_upsert(brand,name,upc,size,uom,vendor,cost,retail,notes,reorder_level):
    if not ((brand or "").strip() or (name or "").strip() or (upc or "").strip()):
        return None

    with db() as con:
        row = con.execute(
            "SELECT id FROM items WHERE brand=? AND name=? AND IFNULL(upc,'')=?",
            (brand, name, upc or "")
        ).fetchone()
        if row:
            item_id = row["id"]
            con.execute("""UPDATE items
                           SET size=?, uom=?, vendor=?, cost=?, retail=?, notes=?, reorder_level=?
                           WHERE id=?""",
                        (size,uom,vendor,cost,retail,notes,reorder_level,item_id))
        else:
            con.execute("""INSERT INTO items(brand,name,upc,size,uom,vendor,cost,retail,notes,reorder_level)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (brand,name,upc,size,uom,vendor,cost,retail,notes,reorder_level))
            item_id = con.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    return item_id

def dao_item_get(item_id):
    with db() as con:
        row = con.execute(
            "SELECT id,brand,name,upc,size,uom,vendor,cost,retail,notes,reorder_level FROM items WHERE id=?",
            (item_id,)
        ).fetchone()
    return dict(row) if row else None

def dao_item_lots(item_id):
    with db() as con:
        rows = con.execute(
            "SELECT id,lot,expiry,qty FROM lots WHERE item_id=? ORDER BY expiry ASC, id ASC",
            (item_id,)
        ).fetchall()
    return [dict(x) for x in rows]

from contextlib import contextmanager
_AUDIT_SUPPRESS = False

@contextmanager
def no_audit():
    global _AUDIT_SUPPRESS
    prev = _AUDIT_SUPPRESS
    _AUDIT_SUPPRESS = True
    try:
        yield
    finally:
        _AUDIT_SUPPRESS = prev

def log_action(user:str, action:str, details:str=""):
    if globals().get("_AUDIT_SUPPRESS"):
        return
    with get_db() as con:
        con.execute(
            "INSERT INTO logs(user, action, details) VALUES (?,?,?)",
            (user or "", action or "", details or "")
        )


# --- DAO compatibility: stock qty wrapper -----------------
def dao_item_stock(item_id: int) -> int:
    """Return total on-hand qty for an item. Self-contained (no other helpers)."""
    with db() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(qty),0) AS q FROM lots WHERE item_id=?",
            (item_id,)
        ).fetchone()
    return int(row["q"] or 0)

def dao_item_stock_qty(item_id):
    with db() as con:
        row = con.execute("SELECT COALESCE(SUM(qty),0) q FROM lots WHERE item_id=?", (item_id,)).fetchone()
    return int(row["q"] or 0)

def dao_add_lot(item_id, lot, expiry_yyyymmdd, qty):
    q = int(qty or 0)
    if q < 0:
        q = 0
    with db() as con:
        con.execute("INSERT INTO lots(item_id,lot,expiry,qty) VALUES(?,?,?,?)",
                    (item_id, lot or None, expiry_yyyymmdd or None, q))
    return True

# --- DAO: search helpers ------------------------------------------------------
def dao_search_by_desc(keywords: str):
    """Search items by brand/name/notes. Returns list of dicts."""
    q = (keywords or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    with db() as con:
        rows = con.execute(
            """SELECT id, brand, name, size, uom, upc
               FROM items
               WHERE IFNULL(brand,'') LIKE ?
                  OR IFNULL(name,'')  LIKE ?
                  OR IFNULL(notes,'') LIKE ?
               ORDER BY brand, name, id""",
            (like, like, like),
        ).fetchall()
    return [dict(r) for r in rows]

def dao_lot_delete(lot_id:int):
    with get_db() as con:
        con.execute("DELETE FROM lots WHERE id=?", (lot_id,))
        con.commit()

def dao_item_delete(item_id:int):
    with get_db() as con:
        # also clear any picklist line-items pointing at this item
        con.execute("DELETE FROM picklist_items WHERE item_id=?", (item_id,))
        con.execute("DELETE FROM lots WHERE item_id=?", (item_id,))
        con.execute("DELETE FROM items WHERE id=?", (item_id,))
        con.commit()

def dao_all_items():
    with get_db() as con:
        rows = con.execute("""
            SELECT i.id, i.brand, i.name, i.upc, i.size, i.uom,
                   COALESCE((SELECT SUM(qty) FROM lots WHERE item_id=i.id), 0) AS stock_qty
            FROM items i
            ORDER BY i.brand, i.name, i.size, i.uom
        """).fetchall()
    return [dict(r) for r in rows]

def dao_search_by_upc(fragment: str):
    """Search items by UPC (partial match)."""
    q = (fragment or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    with db() as con:
        rows = con.execute(
            """SELECT id, brand, name, size, uom, upc
               FROM items
               WHERE IFNULL(upc,'') LIKE ?
               ORDER BY brand, name, id""",
            (like,),
        ).fetchall()
    return [dict(r) for r in rows]

def dao_lots_expiring_between(start_yyyymmdd: str, end_yyyymmdd: str):
    """Return lots with expiry between the two dates inclusive."""
    if not (start_yyyymmdd and end_yyyymmdd):
        return []
    with db() as con:
        rows = con.execute(
            """SELECT l.item_id, i.brand, i.name, i.size, i.uom,
                      l.lot, l.expiry, l.qty
               FROM lots l
               JOIN items i ON i.id = l.item_id
               WHERE l.expiry BETWEEN ? AND ?
               ORDER BY l.expiry ASC, i.brand, i.name""",
            (start_yyyymmdd, end_yyyymmdd),
        ).fetchall()
    return [dict(r) for r in rows]

# (Optional) Back-compat aliases if other parts of the code still use the old names:
def _query_items_by_desc(q):          return dao_search_by_desc(q)
def _query_items_by_upc(q):           return dao_search_by_upc(q)
def _query_items_by_expiry_week(s):   # needs _add_days_yyyymmdd which already exists in your file
    return dao_lots_expiring_between(s, _add_days_yyyymmdd(s, 7))

def _add_days_yyyymmdd(s: str, days: int) -> str:
    """Return YYYYMMDD + N days (still as YYYYMMDD). If invalid, return s unchanged."""
    try:
        y, m, d = int(s[0:4]), int(s[4:6]), int(s[6:8])
        dt = datetime.date(y, m, d) + datetime.timedelta(days=days)
        return dt.strftime("%Y%m%d")
    except Exception:
        return s

# --- Lot helpers / compatibility ------------------------------------------
def add_lot(item_id: int, lot: str, expiry_yyyymmdd: str, qty: int):
    """Create a lot row. Accepts blank lot/expiry; qty coerced to non-negative int."""
    try:
        qty = int(qty or 0)
    except Exception:
        qty = 0
    if qty < 0:
        qty = 0
    with db() as con:
        con.execute(
            "INSERT INTO lots(item_id,lot,expiry,qty) VALUES(?,?,?,?)",
            (item_id, lot or None, expiry_yyyymmdd or None, qty),
        )

# Alias for any code that expects a DAO-style name
def dao_add_lot(item_id: int, lot: str, expiry_yyyymmdd: str, qty: int):
    return add_lot(item_id, lot, expiry_yyyymmdd, qty)

def dao_item_delete(item_id: int):
    """Delete an item, all its lots, and any picklist rows that reference it."""
    with get_db() as con:
        # remove picklist references first to avoid orphan rows
        con.execute("DELETE FROM picklist_items WHERE item_id = ?", (item_id,))
        # delete lots for this item
        con.execute("DELETE FROM lots WHERE item_id = ?", (item_id,))
        # delete the item itself
        con.execute("DELETE FROM items WHERE id = ?", (item_id,))
        con.commit()


# ---------- LOGGING ----------

def log_action(who, action, details=''):
    if action == "log_delete":
        return
    if not who and CURRENT_USER.get("initials"):
        who = CURRENT_USER["initials"]
    ts = datetime.datetime.now().isoformat(timespec='seconds')
    with db() as con:
        con.execute("INSERT INTO logs(ts,who,action,details) VALUES(?,?,?,?)",
                    (ts, who or "?", action, details or ""))

# ---------- DATE HELPERS (no slashes in input) ----------

# --- Lot string -> date (basic) ---------------------------------------------

# --- Date input parser (supports YYYYMMDD or YYYYMM) -----------------------

def parse_date_input(raw: str) -> str:
    """
    Accepts 'YYYYMMDD' (8 digits) or 'YYYYMM' (6 digits). No slashes.
    If 6 digits, returns the last day of that month.
    Returns canonical 'YYYYMMDD' or '' if invalid.
    """
    s = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(s) == 8:
        y, m, d = int(s[0:4]), int(s[4:6]), int(s[6:8])
        try:
            datetime.date(y, m, d)  # validate
            return f"{y:04d}{m:02d}{d:02d}"
        except Exception:
            return ""
    if len(s) == 6:
        y, m = int(s[0:4]), int(s[4:6])
        try:
            last_day = calendar.monthrange(y, m)[1]
            return f"{y:04d}{m:02d}{last_day:02d}"
        except Exception:
            return ""
    return ""

def decode_lot_basic(lot: str) -> str:
    """
    Very basic decoders (no slashes). Returns canonical 'YYYYMMDD' or '' if unknown.
      - 8 digits  : YYYYMMDD
      - 6 digits  : YYMMDD  (pivot: <70 => 2000s, else 1900s)
      - 5 digits  : YDDD    (Julian: last digit of year + day-of-year; pivot to 2020s)
    """
    s = "".join(ch for ch in (lot or "") if ch.isalnum())
    if not s.isdigit():
        return ""

    if len(s) == 8:
        # YYYYMMDD → validate via existing parser
        return parse_date_input(s)

    if len(s) == 6:
        # YYMMDD
        try:
            yy = int(s[0:2])
            year = 2000 + yy if yy < 70 else 1900 + yy
            m = int(s[2:4]); d = int(s[4:6])
            datetime.date(year, m, d)  # validate
            return f"{year:04d}{m:02d}{d:02d}"
        except Exception:
            return ""

    if len(s) == 5:
        # YDDD (Julian)
        try:
            y = 2020 + int(s[0])  # coarse pivot to 2020s
            ddd = int(s[1:])
            base = datetime.date(y, 1, 1)
            dt = base + datetime.timedelta(days=ddd - 1)
            return dt.strftime("%Y%m%d")
        except Exception:
            return ""

    return ""


def fmt_ymd(yyyymmdd: str) -> str:
    s = (yyyymmdd or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}/{s[4:6]}/{s[6:8]}"
    return ""

def days_until(yyyymmdd: str):
    s = (yyyymmdd or "")
    try:
        y,m,d = int(s[0:4]), int(s[4:6]), int(s[6:8])
        tgt = datetime.date(y,m,d)
        return (tgt - datetime.date.today()).days
    except:
        return None

def _add_days_yyyymmdd(s: str, days: int) -> str:
    """Return YYYYMMDD + N days (still as YYYYMMDD). If invalid, return s unchanged."""
    try:
        y, m, d = int(s[0:4]), int(s[4:6]), int(s[6:8])
        dt = datetime.date(y, m, d) + datetime.timedelta(days=days)
        return dt.strftime("%Y%m%d")
    except Exception:
        return s

# ---------- REPORT WRITERS (plain-text printable files) ----------

def _report_filename(stem: str, folder: str):
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch for ch in stem if ch.isalnum() or ch in ("-","_"," ")).rstrip()
    name = f"{safe}--{ts}.txt"
    return os.path.join(folder, name)

def _write_lines_to_file(lines, folder, stem):
    path = _report_filename(stem, folder)
    try:
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line.rstrip("\n") + "\n")
    except Exception as e:
        return None, str(e)
    return path, None

def render_picklist_report_lines(picklist_header: dict, rows: list):
    """
    rows entries should be dicts with keys:
      brand, name, size, uom, req_qty, note (optional), upc (optional)
    """
    lines = []
    lines.append("PICK LIST".center(64))
    lines.append(f"Name: {picklist_header.get('name','')}   "
                 f"Status: {picklist_header.get('status','')}   "
                 f"Created: {picklist_header.get('created_at','')}")
    lines.append("-"*64)
    lines.append(f"{'QTY':>4}  {'BRAND':<16}  {'PRODUCT':<28}  {'SIZE':<7} {'UOM':<3}")
    lines.append("-"*64)
    for r in rows:
        qty = str(r.get("req_qty",0))[:4]
        brand = (r.get("brand") or "")[:16]
        prod  = (r.get("name") or "")[:28]
        size  = (r.get("size") or "")[:7]
        uom   = (r.get("uom") or "")[:3]
        lines.append(f"{qty:>4}  {brand:<16}  {prod:<28}  {size:<7} {uom:<3}")
        if r.get("note"):
            lines.append(f"      note: {r['note']}")
    lines.append("-"*64)
    lines.append(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  (v{APP_VERSION})")
    return lines

def render_low_stock_report_lines(rows: list):
    """
    rows entries with: brand, name, size, uom, reorder_level, stock
    """
    lines = []
    lines.append("LOW STOCK REPORT".center(64))
    lines.append("-"*64)
    lines.append(f"{'STK/RL':<9} {'ID':>4}  {'BRAND':<16}  {'PRODUCT':<28}")
    lines.append("-"*64)
    for r in rows:
        stk = int(r.get("stock",0)); rl = int(r.get("reorder_level",0))
        lines.append(f"{stk:>4}/{rl:<4} {('#'+str(r.get('id',''))):>5}  {(r.get('brand') or ''):<16}  {(r.get('name') or ''):<28}")
    lines.append("-"*64)
    lines.append(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  (v{APP_VERSION})")
    return lines

def render_expiries_report_lines(title: str, rows: list):
    """
    rows entries with: expiry, qty, brand, name, lot
    """
    lines = []
    lines.append(title.upper().center(64))
    lines.append("-"*64)
    lines.append(f"{'EXPIRY':<12} {'QTY':>4}  {'BRAND':<16}  {'PRODUCT':<28}")
    lines.append("-"*64)
    for r in rows:
        lines.append(f"{fmt_ymd(r.get('expiry','')):<12} {str(r.get('qty',0)):>4}  {(r.get('brand') or ''):<16}  {(r.get('name') or ''):<28}")
        if r.get("lot"):
            lines.append(f"           lot: {r['lot']}")
    lines.append("-"*64)
    lines.append(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  (v{APP_VERSION})")
    return lines

def save_picklist_report(picklist_header: dict, rows: list):
    lines = render_picklist_report_lines(picklist_header, rows)
    stem = f"PICKLIST-{picklist_header.get('id','')}-{picklist_header.get('name','')}"
    return _write_lines_to_file(lines, REPORTS_PICKLISTS_DIR, stem)

def save_low_stock_report(rows: list):
    lines = render_low_stock_report_lines(rows)
    return _write_lines_to_file(lines, REPORTS_LOW_STOCK_DIR, "LOW-STOCK")

def save_expiries_report(title: str, rows: list):
    lines = render_expiries_report_lines(title, rows)
    safe_title = "".join(ch for ch in title if ch.isalnum() or ch in ("-","_"," ")).rstrip()
    return _write_lines_to_file(lines, REPORTS_EXPIRIES_DIR, safe_title)

# ---------- THEME / TOASTS (headers only; bodies later) ----------

PALETTE = {
    "base":   10,  # white on blue
    "title":  11,  # cyan  on blue
    "border": 12,  # green on blue
    "select": 13,  # black on cyan
    "warn":   14,  # yellow on blue
    "dim":    15,  # white on blue (used as dim)
}

def init_theme(stdscr):
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(PALETTE["base"],   curses.COLOR_WHITE,  curses.COLOR_BLUE)
    curses.init_pair(PALETTE["title"],  curses.COLOR_CYAN,   curses.COLOR_BLUE)
    curses.init_pair(PALETTE["border"], curses.COLOR_GREEN,  curses.COLOR_BLUE)
    curses.init_pair(PALETTE["select"], curses.COLOR_BLACK,  curses.COLOR_CYAN)
    curses.init_pair(PALETTE["warn"],   curses.COLOR_YELLOW, curses.COLOR_BLUE)
    curses.init_pair(PALETTE["dim"],    curses.COLOR_WHITE,  curses.COLOR_BLUE)
    stdscr.bkgd(' ', curses.color_pair(PALETTE["base"]))
    stdscr.erase()
    stdscr.refresh()

def title_bar(stdscr, level_text: str, title_text: str):
    h, w = stdscr.getmaxyx()
    stdscr.attron(curses.color_pair(PALETTE["title"]) | curses.A_BOLD)
    stdscr.addstr(0, 0, f"MENU: LEVEL: {level_text}".ljust(w - 1))
    stdscr.addstr(2, 1, title_text)
    stdscr.attroff(curses.color_pair(PALETTE["title"]) | curses.A_BOLD)

def draw_box(stdscr, top, left, height, width):
    attr = curses.color_pair(PALETTE["border"])
    stdscr.attron(attr)
    stdscr.hline(top, left + 1,  curses.ACS_HLINE, width - 2)
    stdscr.hline(top + height - 1, left + 1, curses.ACS_HLINE, width - 2)
    stdscr.vline(top + 1, left,         curses.ACS_VLINE, height - 2)
    stdscr.vline(top + 1, left + width - 1, curses.ACS_VLINE, height - 2)
    stdscr.addch(top, left,                         curses.ACS_ULCORNER)
    stdscr.addch(top, left + width - 1,             curses.ACS_URCORNER)
    stdscr.addch(top + height - 1, left,            curses.ACS_LLCORNER)
    stdscr.addch(top + height - 1, left + width - 1,curses.ACS_LRCORNER)
    stdscr.attroff(attr)

def info_toast(stdscr, msg: str, secs: float = 0.9):
    h, w = stdscr.getmaxyx()
    stdscr.attron(curses.color_pair(PALETTE["warn"]) | curses.A_BOLD)
    stdscr.addstr(h - 1, 1, msg[: max(1, w - 3)].ljust(w - 2))
    stdscr.attroff(curses.color_pair(PALETTE["warn"]) | curses.A_BOLD)
    stdscr.refresh()
    time.sleep(secs)

def warn_toast(stdscr, msg: str, secs: float = 1.1):
    info_toast(stdscr, msg, secs)
# ======================================================================
# PART 2/6 — UI widgets, input forms, menus, paginated lists
# ======================================================================

import textwrap

# --- simple dialogs ----------------------------------------------------

def confirm_dialog(stdscr, title='Confirm', message='Are you sure?', yes='Yes', no='No'):
    init_theme(stdscr)
    title_bar(stdscr, path_to_str(CURRENT_PATH), title)

    h, w = stdscr.getmaxyx()
    bw = min(max(len(message) + 12, 44), w - 6)
    bh = 7
    top = max(3, (h - bh) // 2)
    left = max(3, (w - bw) // 2)

    draw_box(stdscr, top, left, bh, bw)
    stdscr.addstr(top + 2, left + 2, message[: bw - 4])

    sel = 0
    while True:
        # Buttons row
        y = top + bh - 2
        x = left + 2
        for i, lab in enumerate((yes, no)):
            label = f"[ {lab} ]"
            attr = curses.A_REVERSE if i == sel else curses.A_NORMAL
            stdscr.addstr(y, x, label, attr)
            x += len(label) + 2
        stdscr.refresh()

        k = stdscr.getch()
        if k in (curses.KEY_LEFT, curses.KEY_RIGHT, 9):  # arrows or Tab toggles
            sel = 1 - sel
        elif k in (10, 13):  # Enter
            return sel == 0
        elif k == curses.KEY_F2:
            return False

def info_center(stdscr, title, text, footer="[Enter] OK   [F2] Back"):
    init_theme(stdscr)
    title_bar(stdscr, path_to_str(CURRENT_PATH), title)
    h, w = stdscr.getmaxyx()

    lines = []
    for para in (text or "").splitlines():
        lines.extend(textwrap.wrap(para, width=max(20, w - 6)) or [""])

    bh = min(max(len(lines) + 6, 8), h - 4)
    bw = min(max(max((len(s) for s in lines), default=0) + 6, 40), w - 4)
    top = max(3, (h - bh) // 2)
    left = max(2, (w - bw) // 2)

    draw_box(stdscr, top, left, bh, bw)
    for i, s in enumerate(lines[: bh - 4]):
        stdscr.addstr(top + 2 + i, left + 2, s[: bw - 4])
    stdscr.addstr(top + bh - 2, left + 2, footer, curses.A_DIM)
    stdscr.refresh()

    while True:
        k = stdscr.getch()
        if k in (10, 13, curses.KEY_F2):
            return

# --- input primitives --------------------------------------------------

def _render_fields(stdscr, top, left, box_w, fields, idx, mask):
    """
    Draw labels and input areas. Adds a visible space after the colon so the
    first typed character doesn't touch the label (fixes 'Notes:R' look).
    """
    y = top + 2
    for i, (label, value) in enumerate(fields):
        v = value or ""
        # handle password-mask lists/sets gracefully
        is_masked = bool(mask and label in (mask if isinstance(mask, (set, list, tuple)) else {mask}))
        show = "*" * len(v) if is_masked else v

        # label with an explicit space after the colon
        label_text = f"{label}: "
        stdscr.addstr(y, left + 2, label_text)

        # input field starts right after that space
        field_x = left + 2 + len(label_text)

        # width available for the input area
        maxw = max(1, box_w - (len(label_text) + 6))

        # highlight active field
        attr = curses.color_pair(PALETTE["select"]) if i == idx else curses.A_DIM

        # draw/pad input area so the highlight fully spans the field
        stdscr.addstr(y, field_x, show[:maxw].ljust(maxw), attr)

        y += 2


def input_form(stdscr, title, fields, footer=None, mask=None, submit_on_tab=False):
    """
    fields: list[(label, default_value)]
    Returns: dict or None on F2
    Behavior: TAB moves to next field; when on last field:
              - Enter submits
              - Tab submits only if submit_on_tab=True, otherwise wraps to first
    """
    init_theme(stdscr)
    title_bar(stdscr, path_to_str(CURRENT_PATH), title)
    h, w = stdscr.getmaxyx()

    box_w = min(max(60, int(w * 0.85)), w - 4)
    box_h = min(max(len(fields) * 2 + 6, 12), h - 4)
    top = max(3, (h - box_h) // 2)
    left = max(2, (w - box_w) // 2)
    draw_box(stdscr, top, left, box_h, box_w)
    if footer:
        stdscr.addstr(top + box_h - 2, left + 2, footer, curses.A_DIM)

    idx = 0
    while True:
        _render_fields(stdscr, top, left, box_w, fields, idx, mask)
        stdscr.refresh()
        k = stdscr.getch()

        if k == curses.KEY_F2:
            return None

        elif k in (curses.KEY_UP,):
            idx = (idx - 1) % len(fields)

        elif k in (curses.KEY_DOWN, 9):  # Down or TAB
            if idx < len(fields) - 1:
                idx += 1
            else:
                if submit_on_tab:
                    return {k: v for (k, v) in fields}
                idx = 0  # wrap

        elif k in (10, 13):  # Enter
            if idx == len(fields) - 1:
                return {k: v for (k, v) in fields}
            else:
                idx += 1

        elif k in (curses.KEY_BACKSPACE, 127, 8):
            label, val = fields[idx]
            val = (val or "")
            if val:
                val = val[:-1]
            fields[idx] = (label, val)

        elif 32 <= k <= 126:
            ch = chr(k)
            label, val = fields[idx]
            fields[idx] = (label, (val or "") + ch)

def input_form_tab_submit(stdscr, title, fields, footer=None, mask=None):
    """
    Variant: Enter on last field wraps to top (no submit). Only TAB on last submits.
    """
    init_theme(stdscr)
    title_bar(stdscr, path_to_str(CURRENT_PATH), title)
    h, w = stdscr.getmaxyx()

    box_w = min(max(64, int(w * 0.85)), w - 4)
    box_h = min(max(len(fields) * 2 + 6, 14), h - 4)
    top  = max(3, (h - box_h) // 2)
    left = max(2, (w - box_w) // 2)
    draw_box(stdscr, top, left, box_h, box_w)
    if footer:
        stdscr.addstr(top + box_h - 2, left + 2, footer, curses.A_DIM)

    idx = 0
    while True:
        _render_fields(stdscr, top, left, box_w, fields, idx, mask)
        stdscr.refresh()
        k = stdscr.getch()

        if k == curses.KEY_F2:
            return None
        elif k in (curses.KEY_UP,):
            idx = (idx - 1) % len(fields)
        elif k in (curses.KEY_DOWN,):
            idx = (idx + 1) % len(fields)
        elif k in (9,):  # TAB
            if idx == len(fields) - 1:
                return {k: v for (k, v) in fields}
            else:
                idx += 1
        elif k in (10, 13):  # Enter
            if idx == len(fields) - 1:
                idx = 0  # wrap to top, DO NOT submit
            else:
                idx += 1
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            lab, val = fields[idx]
            fields[idx] = (lab, (val or "")[:-1])
        elif 32 <= k <= 126:
            ch = chr(k)
            lab, val = fields[idx]
            fields[idx] = (lab, (val or "") + ch)
# --- Review dialog used by screen_add_item -------------------------------
def review_item_dialog(stdscr, data:dict):
    """
    Shows a read-only summary before saving.
    Keys:
      - [Tab]  -> return "save"
      - [Enter]-> return "edit"
      - [F2]   -> return "back"
    """
    init_theme(stdscr)
    title_bar(stdscr, path_to_str(CURRENT_PATH), "REVIEW ITEM")
    lines = [
        f"Brand : {data.get('Brand','')}",
        f"Name  : {data.get('Name','')}",
        f"UPC   : {data.get('UPC','')}",
        f"Size  : {data.get('Size','')}",
        f"UOM   : {data.get('UOM','')}",
        f"Vendor: {data.get('Vendor','')}",
        f"Cost  : {data.get('Cost','')}",
        f"Retail: {data.get('Retail','')}",
        f"Reorder Level: {data.get('Reorder Level','0')}",
        f"Lot   : {data.get('Lot (optional)','')}",
        f"Expiry: {fmt_ymd(data.get('Expiry',''))}",
        f"Notes : {data.get('Notes','')}",
    ]

    h, w = stdscr.getmaxyx()
    bw = min(max(max((len(s) for s in lines), default=0) + 6, 60), w - 6)
    bh = min(len(lines) + 7, h - 4)
    top = max(3, (h - bh) // 2)
    left = max(3, (w - bw) // 2)

    draw_box(stdscr, top, left, bh, bw)
    for i, s in enumerate(lines):
        stdscr.addstr(top + 2 + i, left + 2, s[: bw - 4])

    stdscr.addstr(top + bh - 2, left + 2, "[Tab] Save   [Enter] Edit   [F2] Back", curses.A_DIM)
    stdscr.refresh()

    while True:
        k = stdscr.getch()
        if k == curses.KEY_F2:
            return "back"
        if k in (9,):  # Tab
            return "save"
        if k in (10, 13):  # Enter
            return "edit"

def _hotkey_handle(stdscr, keycode):
    if keycode == curses.KEY_F3:
        screen_month_view(stdscr)
        return True
    return False

# --- menu & list -------------------------------------------------------

def list_paginated(stdscr, title, rows, line_func, page_size=12):
    """
    rows: list of any
    line_func(i, row, width) -> str OR (str, attr)
    Returns selected index in 'rows' or -1 on F2.
    Keys: Up/Down, Enter, N=next page, P=prev page.
    """
    if not rows:
        info_center(stdscr, title, "(no results)")
        return -1

    page = 0
    cursor = 0
    while True:
        init_theme(stdscr); title_bar(stdscr, path_to_str(CURRENT_PATH), title)
        h, w = stdscr.getmaxyx()
        box_w = min(max(80, int(w * 0.95)), w - 4)
        box_h = min(max(page_size + 6, 12), h - 4)
        top = max(3, (h - box_h) // 2); left = max(2, (w - box_w) // 2)
        draw_box(stdscr, top, left, box_h, box_w)
        stdscr.addstr(top + 1, left + 2, "[Up/Down] Move  [Enter] Select  [N] Next  [P] Prev  [F2] Back", curses.A_DIM)

        start = page * page_size
        slice_rows = rows[start:start + page_size]
        for i, r in enumerate(slice_rows):
            text_attr = line_func(start + i, r, box_w - 6)
            if isinstance(text_attr, tuple):
                text, attr = text_attr
            else:
                text, attr = text_attr, curses.A_NORMAL
            attr = curses.color_pair(PALETTE["select"]) if i == cursor else attr
            stdscr.addstr(top + 3 + i, left + 3, str(text)[: box_w - 6].ljust(box_w - 6), attr)

        stdscr.refresh()
        k = stdscr.getch()

        if _hotkey_handle(stdscr, k):
            # just redraw page after returning
            continue

        if k == curses.KEY_F2:
            return -1
        elif k == curses.KEY_UP:
            cursor = (cursor - 1) % max(1, len(slice_rows))
        elif k == curses.KEY_DOWN:
            cursor = (cursor + 1) % max(1, len(slice_rows))
        elif k in (ord('n'), ord('N')):
            if (page + 1) * page_size < len(rows):
                page += 1
                cursor = 0
        elif k in (ord('p'), ord('P')):
            if page > 0:
                page -= 1
                cursor = 0
        elif k in (10, 13):
            return start + cursor
# 

        start = page * page_size
        slice_rows = rows[start:start + page_size]
        for i, r in enumerate(slice_rows):
            text_attr = line_func(start + i, r, box_w - 6)
            if isinstance(text_attr, tuple):
                text, attr = text_attr
            else:
                text, attr = text_attr, curses.A_NORMAL
            attr = curses.color_pair(PALETTE["select"]) if i == cursor else attr
            stdscr.addstr(top + 3 + i, left + 3, str(text)[: box_w - 6].ljust(box_w - 6), attr)

        stdscr.refresh()
        k = stdscr.getch()

        if _hotkey_handle(stdscr, k):
            # just redraw page after returning
            continue

        if k == curses.KEY_F2:
            return -1
        elif k == curses.KEY_UP:
            cursor = (cursor - 1) % max(1, len(slice_rows))
        elif k == curses.KEY_DOWN:
            cursor = (cursor + 1) % max(1, len(slice_rows))
        elif k in (ord('n'), ord('N')):
            if (page + 1) * page_size < len(rows):
                page += 1
                cursor = 0
        elif k in (ord('p'), ord('P')):
            if page > 0:
                page -= 1
                cursor = 0
        elif k in (10, 13):
            return start + cursor
# ======================================================================
# PART 3/6 — Data helpers (items/lots/picklists), expiry buckets,
#            and printable report writers
# ======================================================================

# NOTE: Part 1 already defines: db(), log_action(), fmt_ymd(), parse_date_input()
# We add schema helpers, queries, and filesystem writers here.

# ---------- constants & dirs (under the same folder as this script) ----

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_ROOT = os.path.join(APP_DIR, "reports")
DIR_PICKLISTS = os.path.join(REPORT_ROOT, "picklists")
DIR_LOW_STOCK = os.path.join(REPORT_ROOT, "low_stock")
DIR_EXPIRIES  = os.path.join(REPORT_ROOT, "expiries")

def _ensure_report_dirs():
    for p in (REPORT_ROOT, DIR_PICKLISTS, DIR_LOW_STOCK, DIR_EXPIRIES):
        os.makedirs(p, exist_ok=True)

# ---------- date helpers ------------------------------------------------

def _ymd_today() -> str:
    return datetime.date.today().strftime("%Y%m%d")

def _ymd_add(yyyymmdd: str, days: int) -> str:
    try:
        y, m, d = int(yyyymmdd[0:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8])
        dt = datetime.date(y, m, d) + datetime.timedelta(days=days)
        return dt.strftime("%Y%m%d")
    except Exception:
        return yyyymmdd

# Buckets requested:
#  - 'TODAY'        : exactly today
#  - '0_3'          : today .. today+3
#  - '3_7'          : (today+3) .. (today+7)
#  - '7_14'         : (today+7) .. (today+14)
#  - '14_30'        : (today+14).. (today+30)
def expiry_bucket_range(bucket_key: str):
    t0 = _ymd_today()
    if bucket_key == "TODAY":
        return t0, t0, "Today"
    if bucket_key == "0_3":
        return t0, _ymd_add(t0, 3), "Today to 3 Days"
    if bucket_key == "3_7":
        return _ymd_add(t0, 3), _ymd_add(t0, 7), "3–7 Days"
    if bucket_key == "7_14":
        return _ymd_add(t0, 7), _ymd_add(t0, 14), "7–14 Days"
    if bucket_key == "14_30":
        return _ymd_add(t0, 14), _ymd_add(t0, 30), "14–30 Days"
    # fallback
    return t0, t0, "Today"

# ---------- schema for picklists & picklist_items -----------------------

def ensure_picklist_schema():
    """Safe to run any time."""
    with db() as con:
        con.executescript("""
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS picklists(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'open', -- open|closed
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS picklist_items(
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            picklist_id  INTEGER NOT NULL REFERENCES picklists(id) ON DELETE CASCADE,
            item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            qty          INTEGER NOT NULL DEFAULT 0,
            note         TEXT,
            UNIQUE(picklist_id, item_id) ON CONFLICT ABORT
        );
        """)
    return True

# ---------- item & lot data access -------------------------------------

def dao_item_by_id(item_id: int):
    with db() as con:
        r = con.execute("""SELECT id, brand, name, upc, size, uom, vendor,
                                  cost, retail, notes, reorder_level
                           FROM items
                           WHERE id=?""", (item_id,)).fetchone()
    return dict(r) if r else None

def dao_items_with_stock():
    """All items with total stock (sum of lots)."""
    with db() as con:
        rows = con.execute("""
            SELECT i.id, i.brand, i.name, i.size, i.uom, i.reorder_level,
                   COALESCE(SUM(l.qty), 0) AS stock
            FROM items i
            LEFT JOIN lots l ON l.item_id = i.id
            GROUP BY i.id
            ORDER BY i.brand, i.name, i.id
        """).fetchall()
    return [dict(r) for r in rows]

def dao_low_stock_items():
    rows = dao_items_with_stock()
    lows = [r for r in rows if r["reorder_level"] and r["stock"] <= r["reorder_level"]]
    # sort by how far below, then brand/name
    lows.sort(key=lambda x: (x["stock"] - x["reorder_level"], x.get("brand") or "", x.get("name") or ""))
    return lows

def dao_lots_between(start_yyyymmdd: str, end_yyyymmdd: str):
    """Lots expiring in [start..end] inclusive."""
    with db() as con:
        rows = con.execute("""
            SELECT l.item_id, i.brand, i.name, i.size, i.uom,
                   l.lot, l.expiry, l.qty
            FROM lots l
            JOIN items i ON i.id = l.item_id
            WHERE l.expiry BETWEEN ? AND ?
            ORDER BY l.expiry ASC, i.brand, i.name, l.id
        """, (start_yyyymmdd, end_yyyymmdd)).fetchall()
    return [dict(r) for r in rows]

# ---------- picklist CRUD ----------------------------------------------

def dao_picklists_all():
    with db() as con:
        rows = con.execute("""SELECT id, name, status, created_at, created_by
                              FROM picklists
                              ORDER BY id DESC""").fetchall()
    return [dict(r) for r in rows]

def dao_picklist_create(name: str):
    with db() as con:
        con.execute("""INSERT INTO picklists(name,status,created_at,created_by)
                       VALUES(?,?,?,?)""",
                    (name, "open", datetime.datetime.now().isoformat(timespec='seconds'),
                     CURRENT_USER.get("initials", "")))
    log_action(CURRENT_USER.get("initials",""), "picklist_create", name)
    return True

def dao_picklist_delete(pid: int):
    with db() as con:
        con.execute("DELETE FROM picklists WHERE id=?", (pid,))
    log_action(CURRENT_USER.get("initials",""), "picklist_delete", f"id={pid}")
    return True

def dao_picklist_toggle(pid: int):
    with db() as con:
        r = con.execute("SELECT status FROM picklists WHERE id=?", (pid,)).fetchone()
        if not r:
            return
        new_status = "closed" if r["status"] == "open" else "open"
        con.execute("UPDATE picklists SET status=? WHERE id=?", (new_status, pid))
    log_action(CURRENT_USER.get("initials",""), "picklist_toggle", f"id={pid};{new_status}")
    return True

def dao_picklist_items(pid: int):
    with db() as con:
        rows = con.execute("""
            SELECT pi.id AS pli_id, pi.item_id, pi.qty, pi.note,
                   i.brand, i.name, i.size, i.uom,
                   COALESCE((SELECT SUM(qty) FROM lots WHERE item_id=i.id),0) AS stock,
                   i.reorder_level
            FROM picklist_items pi
            JOIN items i ON i.id = pi.item_id
            WHERE pi.picklist_id=?
            ORDER BY i.brand, i.name, i.id
        """, (pid,)).fetchall()
    return [dict(r) for r in rows]

def dao_picklist_add_item(pid: int, item_id: int, qty: int = 0, note: str = None):
    qty = int(qty or 0)
    with db() as con:
        # Upsert: add or increase qty
        con.execute("""
            INSERT INTO picklist_items(picklist_id, item_id, qty, note)
            VALUES(?,?,?,?)
            ON CONFLICT(picklist_id, item_id) DO UPDATE
              SET qty  = picklist_items.qty + excluded.qty,
                  note = COALESCE(excluded.note, picklist_items.note)
        """, (pid, item_id, qty, note))
    log_action(CURRENT_USER.get("initials",""), "picklist_add_item", f"pid={pid};item={item_id};qty={qty}")
    return True

def dao_picklist_set_qty(pid: int, item_id: int, qty: int):
    qty = max(0, int(qty or 0))
    with db() as con:
        con.execute("""UPDATE picklist_items
                       SET qty=?
                       WHERE picklist_id=? AND item_id=?""",
                    (qty, pid, item_id))
    log_action(CURRENT_USER.get("initials",""), "picklist_set_qty", f"pid={pid};item={item_id};qty={qty}")
    return True

def dao_picklist_remove_item(pid: int, item_id: int):
    with db() as con:
        con.execute("DELETE FROM picklist_items WHERE picklist_id=? AND item_id=?", (pid, item_id))
    log_action(CURRENT_USER.get("initials",""), "picklist_remove_item", f"pid={pid};item={item_id}")
    return True

# ---- Picklist helpers (builders / exporters) -----------------------------

def dao_picklist_get(pid:int):
    with db() as con:
        r = con.execute("""SELECT id,name,status,created_at,created_by
                           FROM picklists WHERE id=?""", (pid,)).fetchone()
    return dict(r) if r else None

def dao_items_low_stock():
    """Return items where stock <= reorder_level with stock and reorder_level present."""
    rows = fetch_items_with_stock()
    return [r for r in rows if r["reorder_level"] and r["stock"] <= r["reorder_level"]]

def build_picklist_from_low_stock(pid:int):
    """Add/merge low-stock items into picklist. Target qty = reorder_level - stock (>=0)."""
    items = dao_items_low_stock()
    if not items:
        return 0
    inserted = 0
    with db() as con:
        for r in items:
            target_qty = max(0, int(r["reorder_level"]) - int(r["stock"]))
            if target_qty <= 0:
                continue
            con.execute("""
                INSERT INTO picklist_items(picklist_id, item_id, qty)
                VALUES(?,?,?)
                ON CONFLICT(picklist_id, item_id)
                DO UPDATE SET qty = picklist_items.qty + excluded.qty
            """, (pid, r["id"], target_qty))
            inserted += 1
    return inserted

def build_picklist_from_expiries(pid:int, start_ymd:str, end_ymd:str):
    """Add/merge items that have lots expiring between start_ymd and end_ymd (inclusive)."""
    with db() as con:
        rows = con.execute("""
            SELECT l.item_id, COALESCE(SUM(l.qty),0) AS q
            FROM lots l
            WHERE l.expiry BETWEEN ? AND ?
            GROUP BY l.item_id
        """, (start_ymd, end_ymd)).fetchall()
        added = 0
        for r in rows:
            q = int(r["q"] or 0)
            if q <= 0:
                continue
            con.execute("""
                INSERT INTO picklist_items(picklist_id, item_id, qty)
                VALUES(?,?,?)
                ON CONFLICT(picklist_id, item_id)
                DO UPDATE SET qty = picklist_items.qty + excluded.qty
            """, (pid, r["item_id"], q))
            added += 1
    return added

def prompt_expiry_range(stdscr):
    """Return (start_ymd, end_ymd) or (None, None) on back."""
    today = datetime.date.today()

    options = [
        ("Overdue (before today)", "past"),
        ("Today's expires",                 (0, 0)),
        ("0–3 days (incl today)",           (0, 3)),
        ("3–7 days",                        (3, 7)),
        ("7–14 days",                       (7, 14)),
        ("14–30 days",                     (14, 30)),
    ]

    idx = menu(stdscr, "CHOOSE EXPIRY RANGE", [o[0] for o in options])
    if idx == -1:
        return None, None

    sel = options[idx][1]
    if sel == "past":
        start = "19000101"
        end   = (today - datetime.timedelta(days=1)).strftime("%Y%m%d")
    else:
        start_off, end_off = sel
        start = (today + datetime.timedelta(days=start_off)).strftime("%Y%m%d")
        end   = (today + datetime.timedelta(days=end_off)).strftime("%Y%m%d")

    return start, end


def export_picklist_pdf(pid: int):
    """Fallback: reuse TXT."""
    return export_picklist_txt(pid)

    os.makedirs(REPORTS_PICKLISTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_PICKLISTS_DIR, f"picklist_{pid:04d}.txt")

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"PICK LIST #{pid}  [{header['status']}]  {header['name']}\n")
        f.write(f"Created: {header['created_by']} @ {header['created_at']}\n")
        f.write("-" * 90 + "\n")
        f.write("ID   QTY   BRAND  NAME  SIZE UOM   UPC\n")
        for r in rows:
            f.write(f"{r['item_id']:>4}  {int(r['qty']):>4}  "
                    f"{(r.get('brand') or '')}  {(r.get('name') or '')}  "
                    f"{(r.get('size') or '')} {(r.get('uom') or '')}   {(r.get('upc') or '')}\n")
    return path

def export_picklist_pdf(pid:int):
    """
    Fallback: we output the same TXT (no external libs needed).
    Keeping the function so the [F] key works; it returns the TXT path.
    """
    return export_picklist_txt(pid)


# ---------- builders: from low stock & from expiry windows --------------

def build_picklist_from_low_stock(pid: int, add_qty: bool = False):
    """
    Add all low-stock items to picklist. If add_qty=True, suggest qty to reach reorder_level.
    """
    lows = dao_low_stock_items()
    with db() as con:
        for r in lows:
            target_qty = 0
            if add_qty:
                # suggested qty to reach reorder level (never negative)
                target_qty = max(0, int(r["reorder_level"]) - int(r["stock"]))
            con.execute("""
                INSERT INTO picklist_items(picklist_id, item_id, qty)
                VALUES(?,?,?)
                ON CONFLICT(picklist_id, item_id) DO UPDATE
                    SET qty = CASE
                                WHEN excluded.qty > picklist_items.qty THEN excluded.qty
                                ELSE picklist_items.qty
                              END
            """, (pid, r["id"], target_qty))
    log_action(CURRENT_USER.get("initials",""), "picklist_build_low_stock", f"pid={pid};count={len(lows)}")
    return len(lows)

def build_picklist_from_expiries(pid: int, start_ymd: str, end_ymd: str):
    """
    Adds items that have any lot expiring in [start..end].
    Quantity defaults to 0 (decide at picking time).
    """
    lots = dao_lots_between(start_ymd, end_ymd)
    seen = set()
    added = 0
    with db() as con:
        for r in lots:
            itm = r["item_id"]
            if itm in seen:
                continue
            seen.add(itm)
            con.execute("""
                INSERT INTO picklist_items(picklist_id, item_id, qty)
                VALUES(?,?,0)
                ON CONFLICT(picklist_id, item_id) DO NOTHING
            """, (pid, itm))
            added += 1
    log_action(CURRENT_USER.get("initials",""), "picklist_build_expiries",
               f"pid={pid};range={start_ymd}-{end_ymd};items={added}")
    return added

# ---------- printable helpers ------------------------------------------

def _ts():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

def _fw(s, width, align="left"):
    s = "" if s is None else str(s)
    if len(s) > width:
        return s[:max(0, width-1)] + "…"
    if align == "right":
        return s.rjust(width)
    if align == "center":
        pad = max(0, width - len(s))
        left = pad // 2
        right = pad - left
        return " " * left + s + " " * right
    return s.ljust(width)

def _write_txt(path, lines):
    _ensure_report_dirs()
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line.rstrip("\n") + "\n")
    return path

# ---------- printable: picklist ----------------------------------------

def save_picklist_print(pid: int):
    rows = dao_picklist_items(pid)
    # header
    today = datetime.date.today().strftime("%Y-%m-%d")
    title = f"PICK LIST #{pid} — {today}"
    sub   = f"Prepared by: {CURRENT_USER.get('initials','')}    Status: (see app)"
    cols  = [("TO PICK",6), ("BRAND",14), ("NAME",26), ("SIZE",8), ("UOM",5), ("STOCK",6), ("NOTE",18)]
    sep   = "─" * (sum(w for _,w in cols) + (len(cols)-1) * 3)

    out = []
    out.append(title)
    out.append(sub)
    out.append(sep)
    # header row
    out.append(" | ".join(_fw(h, w, "center") for h, w in cols))
    out.append(sep)

    if not rows:
        out.append("(No items on this pick list yet.)")
    else:
        for r in rows:
            line = [
                _fw("", 6, "center"),  # blank to handwrite quantity
                _fw(r.get("brand",""), 14),
                _fw(r.get("name",""), 26),
                _fw(r.get("size",""), 8),
                _fw(r.get("uom",""), 5),
                _fw(r.get("stock",0), 6, "right"),
                _fw(r.get("note","") or "", 18),
            ]
            out.append(" | ".join(line))

    out.append(sep)
    out.append("Signer: ______________________    Date: ____________")
    out.append("Notes:  __________________________________________________________")
    filename = f"picklist_{pid}_{_ts()}.txt"
    path = os.path.join(DIR_PICKLISTS, filename)
    _write_txt(path, out)
    log_action(CURRENT_USER.get("initials",""), "report_picklist_saved", f"pid={pid};file={os.path.basename(path)}")
    return path

# ---------- printable: low-stock report --------------------------------

def save_low_stock_report():
    lows = dao_low_stock_items()
    today = datetime.date.today().strftime("%Y-%m-%d")
    title = f"LOW STOCK REPORT — {today}"
    sub   = f"Prepared by: {CURRENT_USER.get('initials','')}"
    cols  = [("STOCK",6), ("MIN",5), ("DELTA",6), ("ID",5), ("BRAND",16), ("NAME",28), ("SIZE",8), ("UOM",5)]
    sep   = "─" * (sum(w for _,w in cols) + (len(cols)-1) * 3)

    out = [title, sub, sep, " | ".join(_fw(h, w, "center") for h, w in cols), sep]

    if not lows:
        out.append("(Nothing is below or equal to its reorder level.)")
    else:
        for r in lows:
            delta = int(r["stock"]) - int(r["reorder_level"])
            line = [
                _fw(r["stock"], 6, "right"),
                _fw(r["reorder_level"], 5, "right"),
                _fw(delta, 6, "right"),
                _fw(r["id"], 5, "right"),
                _fw(r.get("brand",""), 16),
                _fw(r.get("name",""), 28),
                _fw(r.get("size",""), 8),
                _fw(r.get("uom",""), 5),
            ]
            out.append(" | ".join(line))

    out.append(sep)
    filename = f"low_stock_{_ts()}.txt"
    path = os.path.join(DIR_LOW_STOCK, filename)
    _write_txt(path, out)
    log_action(CURRENT_USER.get("initials",""), "report_low_stock_saved", os.path.basename(path))
    return path

# ---------- printable: nearing-expiries report -------------------------

def save_expiry_report(bucket_key: str):
    start, end, label = expiry_bucket_range(bucket_key)
    lots = dao_lots_between(start, end)

    today = datetime.date.today().strftime("%Y-%m-%d")
    title = f"NEARING EXPIRIES — {label} — Generated {today}"
    sub   = f"Prepared by: {CURRENT_USER.get('initials','')}"
    cols  = [("EXPIRY",12), ("QTY",5), ("BRAND",16), ("NAME",28), ("LOT",14), ("SIZE",8), ("UOM",5)]
    sep   = "─" * (sum(w for _,w in cols) + (len(cols)-1) * 3)

    out = [title, sub, f"Window: {fmt_ymd(start)} .. {fmt_ymd(end)}", sep,
           " | ".join(_fw(h, w, "center") for h, w in cols), sep]

    if not lots:
        out.append("(No lots found in this window.)")
    else:
        for r in lots:
            line = [
                _fw(fmt_ymd(r["expiry"]), 12),
                _fw(r["qty"], 5, "right"),
                _fw(r.get("brand",""), 16),
                _fw(r.get("name",""), 28),
                _fw(r.get("lot","") or "", 14),
                _fw(r.get("size",""), 8),
                _fw(r.get("uom",""), 5),
            ]
            out.append(" | ".join(line))

    out.append(sep)
    filename = f"expiries_{bucket_key.lower()}_{_ts()}.txt"
    path = os.path.join(DIR_EXPIRIES, filename)
    _write_txt(path, out)
    log_action(CURRENT_USER.get("initials",""), "report_expiries_saved",
               f"{bucket_key}:{os.path.basename(path)}")
    return path
# ======================================================================
# PART 4/6 — Picklist screens, build-from (Low Stock & Expiries),
#            and printable actions
# ======================================================================

# ---- quick prompt helpers ------------------------------------------------

def _prompt_qty(stdscr, title="QUANTITY", seed="0"):
    f = input_form(stdscr, title, [("Qty", seed)], footer="[Enter] OK   [F2] Cancel")
    if f is None:
        return None
    try:
        return max(0, int((f["Qty"] or "0").strip()))
    except Exception:
        warn_toast(stdscr, "Quantity must be a whole number.")
        return None

def _prompt_note(stdscr, title="NOTE", seed=""):
    f = input_form(stdscr, title, [("Note (optional)", seed)], footer="[Enter] OK   [F2] Cancel")
    if f is None:
        return None
    return (f["Note (optional)"] or "").strip()

# ---- expiry bucket chooser (for printable & build-from-expiries) ---------

_BUCKETS = [
    ("TODAY",  "Today only"),
    ("0_3",    "Today → 3 days"),
    ("3_7",    "3 → 7 days"),
    ("7_14",   "7 → 14 days"),
    ("14_30",  "14 → 30 days"),
]

def _choose_expiry_bucket(stdscr, title="CHOOSE EXPIRY WINDOW"):
    labels = [f"{lab}  ({desc})" for lab, desc in _BUCKETS]
    idx = menu(stdscr, title, labels + ["Back"])
    if idx == -1 or idx == len(labels):
        return None
    return _BUCKETS[idx][0]

# ---- picklist item list screen ------------------------------------------

def _render_picklist_item_line(i, r, maxw):
    # show qty to pick, stock, and item info
    qty = str(r.get("qty", 0))
    stock = str(r.get("stock", 0))
    brand = r.get("brand", "") or ""
    name  = r.get("name", "") or ""
    size  = r.get("size", "") or ""
    uom   = r.get("uom", "") or ""
    note  = r.get("note", "") or ""
    s = f"{qty:>4}  [{stock:>4}]  {brand}  {name}  {size} {uom}  {('— ' + note) if note else ''}"
    return s[:maxw]

def screen_picklist_items(stdscr, pid: int, header: str):
    """Manage items on a specific picklist."""
    while True:
        rows = dao_picklist_items(pid)
        init_theme(stdscr); title_bar(stdscr, path_to_str(CURRENT_PATH), f"PICKLIST #{pid} — ITEMS")
        h, w = stdscr.getmaxyx()
        box_w = min(max(96, int(w*0.95)), w-4)
        box_h = min(max(18, 22), h-4)
        top = max(3, (h-box_h)//2); left = max(2, (w-box_w)//2)
        draw_box(stdscr, top, left, box_h, box_w)
        help1 = "[Up/Down] Move  [A] Add  [Q] Set Qty  [S] Note  [R] Remove  [L] Build Low  [X] Build Expiry  [P] Save TXT  [F2] Back"
        stdscr.addstr(top+1, left+2, help1, curses.A_DIM)
        stdscr.addstr(top+2, left+2, header)

        # table header
        stdscr.addstr(top+3, left+2, "   QTY   [STCK]   BRAND / NAME / SIZE UOM   — NOTE")
        # list
        cursor = 0
        while True:
            rows = dao_picklist_items(pid)
            # clear list area
            for i in range(box_h-8):
                stdscr.addstr(top+4+i, left+2, " " * (box_w-4))
            for i, r in enumerate(rows[:box_h-8]):
                attr = curses.color_pair(PALETTE["select"]) if i == cursor else curses.A_NORMAL
                line = _render_picklist_item_line(i, r, box_w-6)
                stdscr.addstr(top+4+i, left+2, line.ljust(box_w-4), attr)
            stdscr.addstr(top+box_h-2, left+2, "[Enter]=Edit Qty/Note for highlighted  |  [F2] Back", curses.A_DIM)
            stdscr.refresh()

            k = stdscr.getch()
            if k == curses.KEY_F2:
                return
            elif k == curses.KEY_UP:
                cursor = max(0, cursor-1)
            elif k == curses.KEY_DOWN:
                cursor = min(max(0, len(rows)-1), cursor+1)
            elif k in (ord('a'), ord('A')):
                # add item via search
                item_id = pick_item_by_search(stdscr, "ADD ITEM TO PICKLIST")
                if item_id:
                    qty = _prompt_qty(stdscr, "SET QTY FOR NEW ITEM", "0")
                    if qty is None: 
                        continue
                    note = _prompt_note(stdscr, "OPTIONAL NOTE", "")
                    dao_picklist_add_item(pid, item_id, qty, note)
                    info_toast(stdscr, "Item added.")
            elif k in (ord('q'), ord('Q')):
                if not rows: 
                    continue
                row = rows[cursor]
                qty = _prompt_qty(stdscr, f"SET QTY — #{row['item_id']}", str(row.get("qty",0)))
                if qty is None:
                    continue
                dao_picklist_set_qty(pid, row["item_id"], qty)
                info_toast(stdscr, "Quantity updated.")
            elif k in (ord('s'), ord('S')):
                if not rows:
                    continue
                row = rows[cursor]
                note = _prompt_note(stdscr, f"SET NOTE — #{row['item_id']}", row.get("note") or "")
                if note is None:
                    continue
                with db() as con:
                    con.execute("UPDATE picklist_items SET note=? WHERE id=?", (note, row["pli_id"]))
                log_action(CURRENT_USER.get("initials",""), "picklist_set_note", f"pid={pid};item={row['item_id']}")
                info_toast(stdscr, "Note updated.")
            elif k in (ord('r'), ord('R')):
                if not rows:
                    continue
                row = rows[cursor]
                if confirm_dialog(stdscr, "Remove Item", f"Remove {row.get('brand','')} {row.get('name','')}?"):
                    dao_picklist_remove_item(pid, row["item_id"])
                    info_toast(stdscr, "Removed.")
                    cursor = max(0, cursor-1)
            elif k in (ord('l'), ord('L')):
                # build from low stock
                if confirm_dialog(stdscr, "Build from Low Stock", "Add suggested quantities to reach minimum?"):
                    added = build_picklist_from_low_stock(pid, add_qty=True)
                else:
                    added = build_picklist_from_low_stock(pid, add_qty=False)
                info_toast(stdscr, f"Added/Updated {added} items.")
            elif k in (ord('x'), ord('X')):
                b = _choose_expiry_bucket(stdscr, "BUILD FROM EXPIRIES")
                if b:
                    start, end, _ = expiry_bucket_range(b)
                    added = build_picklist_from_expiries(pid, start, end)
                    info_toast(stdscr, f"Added {added} items from expiry window.")
            elif k in (ord('p'), ord('P')):
                path = save_picklist_print(pid)
                info_center(stdscr, "PICKLIST SAVED", f"Saved to:\n{path}\n\nYou can open/print the TXT file.", "[Enter] OK   [F2] Back")
            elif k in (10, 13):  # Enter: quick edit qty/note
                if not rows:
                    continue
                row = rows[cursor]
                qty = _prompt_qty(stdscr, f"SET QTY — #{row['item_id']}", str(row.get("qty",0)))
                if qty is None:
                    continue
                dao_picklist_set_qty(pid, row["item_id"], qty)
                note = _prompt_note(stdscr, "OPTIONAL NOTE", row.get("note") or "")
                if note is not None:
                    with db() as con:
                        con.execute("UPDATE picklist_items SET note=? WHERE id=?", (note, row["pli_id"]))
                    log_action(CURRENT_USER.get("initials",""), "picklist_set_note", f"pid={pid};item={row['item_id']}")
                info_toast(stdscr, "Saved.")
            # loop shows list again

# --- Simple picklist viewer opened from the Pick Lists screen --------------
def open_picklist(stdscr, pid: int):
    push_path(f"PDT/PICKLIST#{pid}")
    try:
        pl = dao_picklist_get(pid)
        if not pl:
            warn_toast(stdscr, f"Picklist #{pid} not found.")
            return

        rows = dao_picklist_items_list(pid)

        def _render(i, r, maxw):
            # Example line: "   3 x  #0012  Brand  Name  100 mL"
            s = f"{r['qty']:>4} x  #{r['item_id']:>4}  {(r['brand'] or '')}  {(r['name'] or '')}  {(r['size'] or '')} {(r['uom'] or '')}"
            return s[:maxw]

        title = f"PICKLIST #{pl['id']} — {pl['name']} [{pl['status']}]"
        _ = list_paginated(stdscr, title, rows, _render, page_size=16)  # read-only; F2 backs out
    finally:
        pop_path()


# ---- picklists overview screen -----------------------------------------
# ---------- report writers: picklists ----------
def export_picklist_txt(pid: int):
    header = dao_picklist_get(pid)
    rows   = dao_picklist_items_list(pid)

    os.makedirs(EXPORT_PICKLISTS_DIR, exist_ok=True)
    path = os.path.join(EXPORT_PICKLISTS_DIR, f"picklist_{pid:04d}.txt")

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"PICK LIST #{pid}  [{header['status']}]  {header['name']}\n")
        f.write(f"Created: {header['created_by']} @ {header['created_at']}\n")
        f.write("-" * 90 + "\n")
        f.write("ID   QTY   BRAND  NAME  SIZE UOM   UPC\n")
        for r in rows:
            f.write(f"{r['item_id']:>4}  {int(r['qty']):>4}  "
                    f"{(r.get('brand') or '')}  {(r.get('name') or '')}  "
                    f"{(r.get('size') or '')} {(r.get('uom') or '')}   {(r.get('upc') or '')}\n")
    return path

def export_picklist_pdf(pid: int):
    # If you're using the TXT as a PDF fallback:
    return export_picklist_txt(pid)


def screen_picklists(stdscr):
    push_path("PDT/PICKLISTS")
    ensure_picklists_table()
    ensure_picklist_items_table()
    try:
        cursor = 0
        while True:
            # fetch with item count
            with db() as con:
                rows = con.execute("""
                    SELECT p.id, p.name, p.status, p.created_at, p.created_by,
                           COALESCE(COUNT(pi.id),0) AS item_count
                    FROM picklists p
                    LEFT JOIN picklist_items pi ON pi.picklist_id = p.id
                    GROUP BY p.id
                    ORDER BY p.id DESC
                """).fetchall()
            rows = [dict(r) for r in rows]

            init_theme(stdscr); title_bar(stdscr, path_to_str(CURRENT_PATH), "PICK LISTS")
            h, w = stdscr.getmaxyx()
            bw = min(max(90, int(w * 0.92)), w - 4)
            bh = min(max(18, 22), h - 4)
            top = max(3, (h - bh) // 2); left = max(2, (w - bw) // 2)
            draw_box(stdscr, top, left, bh, bw)

            help_line = ("[Up/Down] Move  [Enter] Open  [N] New  [T] Toggle  [D] Delete  "
                         "[B] Build Low  [E] Build Expiry  [P] Save TXT  [F] Save PDF  [F2] Back")
            stdscr.addstr(top + 1, left + 2, help_line[:bw-4], curses.A_DIM)

            # header
            header = " ID   [STATUS]   NAME                                (created_by @ created_at)           items"
            stdscr.addstr(top + 2, left + 2, header[:bw-4])

            if not rows:
                # friendly empty state
                msg = "No pick lists yet — press [N] to create one."
                stdscr.addstr(top + (bh // 2), left + 2, msg[:bw-4], curses.A_DIM)
                stdscr.refresh()

                k = stdscr.getch()
                if k == curses.KEY_F2:
                    return
                elif k in (ord('n'), ord('N')):
                    nf = input_form(stdscr, "NEW PICKLIST", [("Name", "")],
                                    footer="[Enter] Create   [F2] Back")
                    if nf is None:
                        continue
                    nm = (nf["Name"] or "").strip()
                    if nm:
                        picklist_create(nm)
                        info_toast(stdscr, "Created.")
                    continue
                else:
                    continue  # loop to keep redrawing until something changes

            # draw rows
            max_rows = bh - 6
            for i, r in enumerate(rows[:max_rows]):
                line = (f"{r['id']:>4}  [{r['status']:<6}]  "
                        f"{(r['name'] or ''):<34.34}  "
                        f"({r['created_by'] or ''} @ {r['created_at']})  "
                        f"{r['item_count']:>5}")
                attr = curses.color_pair(PALETTE["select"]) if i == cursor else curses.A_NORMAL
                stdscr.addstr(top + 3 + i, left + 2, line[:bw-4].ljust(bw-4), attr)

            stdscr.refresh()

            # input handling
            k = stdscr.getch()
            if k == curses.KEY_F2:
                return
            elif k == curses.KEY_UP:
                cursor = max(0, cursor - 1)
            elif k == curses.KEY_DOWN:
                cursor = min(max(0, len(rows) - 1), cursor + 1)
            elif k in (10, 13):  # open
                pid = rows[cursor]["id"]
                open_picklist(stdscr, pid)  # your existing detail screen; leave as-is
            elif k in (ord('t'), ord('T')):
                if rows:
                    picklist_toggle(rows[cursor]["id"])
            elif k in (ord('n'), ord('N')):
                nf = input_form(stdscr, "NEW PICKLIST", [("Name", "")],
                                footer="[Enter] Create   [F2] Back")
                if nf is None:
                    continue
                nm = (nf["Name"] or "").strip()
                if nm:
                    picklist_create(nm)
                    info_toast(stdscr, "Created.")
            elif k in (ord('d'), ord('D')):
                if not rows:
                    continue
                pid = rows[cursor]["id"]
                if confirm_dialog(stdscr, "Delete Picklist", f"Delete #{pid}?"):
                    picklist_delete(pid)
                    info_toast(stdscr, "Deleted.")
                    cursor = 0
            # --- Build/Export actions ---
            elif k in (ord('b'), ord('B')):  # build from Low Stock into selected/open picklist
                if rows:
                    pid = rows[cursor]["id"]
                    build_picklist_from_low_stock(pid)
                    info_toast(stdscr, "Added low-stock items.")
                continue

            elif k in (ord('e'), ord('E')):  # build from Expiries using your interactive ranges
                if rows:
                    pid = rows[cursor]["id"]
                    # choose a range (uses your existing prompt function)
                    start_ymd, end_ymd = prompt_expiry_range(stdscr)
                    if start_ymd and end_ymd:
                        build_picklist_from_expiries(pid, start_ymd, end_ymd)
                        info_toast(stdscr, "Added expiry-range items.")
                continue

            elif k in (ord('p'), ord('P')):  # save TXT
                if rows:
                    pid = rows[cursor]["id"]
                    export_picklist_txt(pid)
                    info_toast(stdscr, "Saved .txt to exports/picklists.")
                    continue

            elif k in (ord('f'), ord('F')):  # save PDF
                if rows:
                    pid = rows[cursor]["id"]
                    export_picklist_pdf(pid)
                    info_toast(stdscr, "Saved .pdf to exports/picklists.")
                    continue

    finally:
        pop_path()

# ======================= MONTH VIEW (F3) ============================
# Calendar overview + per-day details

def dao_expiry_counts_by_day(start_ymd: str, end_ymd: str):
    """Return {YYYYMMDD: total_qty} for the date range."""
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT expiry, SUM(qty) AS qty
            FROM lots
            WHERE expiry >= ? AND expiry <= ?
            GROUP BY expiry
            ORDER BY expiry
            """,
            (start_ymd, end_ymd),
        ).fetchall()
    return {r["expiry"]: (r["qty"] or 0) for r in rows}

def dao_items_expiring_on(ymd: str):
    """List items that expire on a specific day with summed qty."""
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        return con.execute(
            """
            SELECT i.id, i.brand, i.name, COALESCE(SUM(l.qty),0) AS qty
            FROM lots l
            JOIN items i ON i.id = l.item_id
            WHERE l.expiry = ?
            GROUP BY i.id, i.brand, i.name
            ORDER BY i.brand, i.name
            """,
            (ymd,),
        ).fetchall()

def screen_day_expiries(stdscr, ymd: str):
    """Simple viewer for a single day's expiries."""
    init_theme(stdscr)
    push_path(f"MONTH/{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}")
    try:
        title_bar(stdscr, path_to_str(CURRENT_PATH), "EXPIRIES ON DAY")
        rows = dao_items_expiring_on(ymd)

        h, w = stdscr.getmaxyx()
        top, left = 3, 2
        draw_box(stdscr, top, left, max(6, h - 6), max(20, w - 4))
        stdscr.addstr(top, left + 2, f"{ymd}   [F2] Back", curses.A_DIM)

        y = top + 2
        if not rows:
            stdscr.addstr(y, left + 2, "No items expire on this day.")
        else:
            stdscr.addstr(y, left + 2, "QTY  BRAND / NAME")
            y += 1
            for r in rows:
                line = f"{str(r['qty']).rjust(3)}  {r['brand']} {r['name']}"
                stdscr.addstr(y, left + 2, line[: w - 6])
                y += 1
                if y >= h - 3:
                    break

        stdscr.refresh()
        while True:
            k = stdscr.getch()
            if k in (curses.KEY_F2, 10, 13, 27):  # F2, Enter, Esc
                return
    finally:
        pop_path()

def screen_month_view(stdscr):
    """Calendar-style month view with per-day totals; Enter opens day details."""
    init_theme(stdscr)
    today = datetime.date.today()
    year, month = today.year, today.month
    cursor_day = min(today.day, 28)  # keep it inside grid by default

    push_path("MONTH")
    try:
        while True:
            first = datetime.date(year, month, 1)
            _, days_in_month = calendar.monthrange(year, month)
            start_ymd = f"{year:04d}{month:02d}01"
            end_ymd   = f"{year:04d}{month:02d}{days_in_month:02d}"
            counts = dao_expiry_counts_by_day(start_ymd, end_ymd)

            h, w = stdscr.getmaxyx()
            stdscr.clear()
            title_bar(stdscr, path_to_str(CURRENT_PATH), f"MONTH VIEW – {first.strftime('%B %Y')}")
            stdscr.addstr(3, 2, "[←/→] Day  [↑/↓] Week  [N] Next month  [P] Prev month  [Enter] Open  [F2] Back", curses.A_DIM)


            top, left = 5, 2
            col_w = max(9, (w - 4) // 7)
            # day names (Mon-first)
            for i, name in enumerate(["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]):
                stdscr.addstr(top, left + i * col_w, name)

            first_weekday = first.weekday()  # Mon=0..Sun=6
            row, col = 0, first_weekday
            d = 1
            while d <= days_in_month:
                y = top + 2 + row * 2
                x = left + col * col_w
                ymd = f"{year:04d}{month:02d}{d:02d}"
                label = f"{d:2d}"
                if counts.get(ymd):
                    label = f"{label} *{counts[ymd]}"
                attr = curses.A_REVERSE if d == cursor_day else curses.A_NORMAL
                stdscr.addstr(y, x, label[: col_w - 1], attr)

                col += 1
                if col >= 7:
                    col = 0
                    row += 1
                d += 1

            k = stdscr.getch()
            if k == curses.KEY_F2:
                return  # back

            elif k == curses.KEY_LEFT:
                # move one day left (stay within this month)
                cursor_day = max(1, cursor_day - 1)

            elif k == curses.KEY_RIGHT:
                # move one day right (stay within this month)
                cursor_day = min(days_in_month, cursor_day + 1)

            elif k == curses.KEY_UP:
                # move one week up
                cursor_day = max(1, cursor_day - 7)

            elif k == curses.KEY_DOWN:
                # move one week down
                cursor_day = min(days_in_month, cursor_day + 7)

            elif k in (ord('n'), ord('N')):
                # next month, keep the same day number where possible
                year2, month2 = year, month + 1
                if month2 == 13:
                    year2, month2 = year + 1, 1
                last = calendar.monthrange(year2, month2)[1]
                year, month = year2, month2
                days_in_month = last
                cursor_day = min(cursor_day, days_in_month)

            elif k in (ord('p'), ord('P')):
                # previous month, keep the same day number where possible
                year2, month2 = year, month - 1
                if month2 == 0:
                    year2, month2 = year - 1, 12
                last = calendar.monthrange(year2, month2)[1]
                year, month = year2, month2
                days_in_month = last
                cursor_day = min(cursor_day, days_in_month)

            elif k in (10, 13):  # Enter
                chosen = datetime.date(year, month, cursor_day)
                ymd = f"{chosen.year:04d}{chosen.month:02d}{chosen.day:02d}"
                screen_day_view(stdscr, ymd)
                continue


                opener = (globals().get("open_day_view")
                          or globals().get("screen_items_by_expiry_date")
                          or globals().get("screen_query_by_expiry_date"))

                if opener:
                    try:
                        opener(stdscr, ymd)   # functions that want 'YYYYMMDD'
                    except TypeError:
                        opener(stdscr, chosen)  # functions that want a date object
                else:
                    info_toast(stdscr, f"{chosen:%Y-%m-%d}")
    finally:
        pop_path()

# ===================== END MONTH VIEW (F3) ==========================


# helper: get all lots expiring exactly on one day (YYYYMMDD) incl. UPC
def _lots_on_day(ymd: str):
    import sqlite3  # safe if already imported
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT
                i.id              AS item_id,
                IFNULL(i.upc,'')  AS upc,
                i.brand, i.name,
                IFNULL(l.lot,'')  AS lot,
                l.qty,
                l.expiry
            FROM lots l
            JOIN items i ON i.id = l.item_id
            WHERE l.expiry = ?
            ORDER BY i.brand, i.name, IFNULL(l.lot,'')
            """,
            (ymd,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# --- Day view: list all lots expiring on a given date ---------------------
def screen_day_view(stdscr, ymd: str):
    push_path(f"MONTH/{ymd}")
    try:
        while True:
            rows = _lots_on_day(ymd)
            title = f"EXPIRES ON {ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"

            if not rows:
                info_center(stdscr, title, "(no items)")
                return

            # Build labels: UPC first
            labels = []
            for r in rows:
                upc = r.get("upc") or ""
                brand = r.get("brand", "")
                name = r.get("name", "")
                lot  = r.get("lot") or "None"
                qty  = r.get("qty", 0)
                exp  = fmt_ymd(r.get("expiry", ""))
                labels.append(f"{upc}  {brand} {name}  [Lot {lot}]  qty:{qty}  exp:{exp}")

            sel = menu(stdscr, title, labels + ["Back"])
            if sel == -1 or sel == len(labels):
                return

            r = rows[sel]
            # Action menu for the selected lot
            while True:
                choice = menu(stdscr, "LOT ACTIONS", [
                    "Open item details",
                    "Remove 1",
                    "Remove specific qty…",
                    "Remove ALL (delete lot)",
                    "Back",
                ])
                if choice in (-1, 4):       # Back
                    break
                item_id = r["item_id"]
                lot_key = r.get("lot") or ""

                if choice == 0:
                    # open item details screen if present
                    if "screen_item_details" in globals():
                        screen_item_details(stdscr, item_id)
                    continue

                if choice == 1:
                    new_q = dao_remove_qty_from_lot(item_id, lot_key, 1)
                    info_toast(stdscr, f"Removed 1 → new qty: {new_q}")
                    break  # refresh day list

                if choice == 2:
                    f = input_form(stdscr, "REMOVE QTY", [("Quantity", "")],
                                   footer="[Enter] Remove  [F2] Cancel")
                    if not f:
                        continue
                    try:
                        q = int((f.get("Quantity") or "0").strip())
                    except Exception:
                        warn_toast(stdscr, "Invalid number.")
                        continue
                    if q <= 0:
                        continue
                    new_q = dao_remove_qty_from_lot(item_id, lot_key, q)
                    info_toast(stdscr, f"Removed {q} → new qty: {new_q}")
                    break  # refresh day list

                if choice == 3:
                    if confirm_dialog(stdscr, "DELETE LOT", "Remove all from this lot?"):
                        dao_remove_qty_from_lot(item_id, lot_key, 10**9)
                        info_toast(stdscr, "Lot removed.")
                        break  # refresh day list

            # loop back to refresh the list for this day
    finally:
        pop_path()



# ======================================================================
# PART 5/6 — PPM screens (Add/Query/Decode/Regular Expiry) + F3 hotkey
# ======================================================================

# ---- extra color pairs for consistent red/yellow/green highlights --------

_EXTRA_PAIRS_READY = False
def _ensure_extra_pairs():
    """Guarantee red/yellow/green-on-blue attrs exist even if theme changes."""
    global _EXTRA_PAIRS_READY
    if _EXTRA_PAIRS_READY:
        return
    try:
        curses.init_pair(90, curses.COLOR_RED,    curses.COLOR_BLUE)
        curses.init_pair(91, curses.COLOR_YELLOW, curses.COLOR_BLUE)
        curses.init_pair(92, curses.COLOR_GREEN,  curses.COLOR_BLUE)
    except Exception:
        pass
    _EXTRA_PAIRS_READY = True

def _attr_red():    _ensure_extra_pairs();   return curses.color_pair(90) | curses.A_BOLD
def _attr_yellow(): _ensure_extra_pairs();   return curses.color_pair(91) | curses.A_BOLD
def _attr_green():  _ensure_extra_pairs();   return curses.color_pair(92) | curses.A_BOLD

# Override expiry color helpers to use these pairs globally
def expiry_attr_for_week_view(yyyymmdd: str):
    dd = days_until(yyyymmdd)
    if dd is None: return curses.A_NORMAL
    if dd == 0:    return _attr_red()      # today
    if 1 <= dd <= 7: return _attr_yellow() # next 7 days
    return curses.A_NORMAL

def expiry_attr_for_month_view(yyyymmdd: str):
    dd = days_until(yyyymmdd)
    if dd is None: return curses.A_NORMAL
    if dd <= 14:   return _attr_yellow()   # next 2 weeks
    if dd <= 28:   return _attr_green()    # next 4 weeks
    return curses.A_NORMAL

# ---- global F3: open Month View anywhere --------------------------------
# remembers last cursor per path
_MENU_CURSOR = globals().get("_MENU_CURSOR", {})

def menu(stdscr, title, items):
    """
    Returns index (0..n-1) or -1 if F2/back.
    Keys: Up/Down, Enter, F2 Back, F3 Month View (via _hotkey_handle).
    """
    init_theme(stdscr); title_bar(stdscr, path_to_str(CURRENT_PATH), title)
    h, w = stdscr.getmaxyx()
    box_w = min(max(54, int(w * 0.70)), w - 4)
    box_h = min(max(len(items) + 6, 12), h - 4)
    top = max(3, (h - box_h) // 2); left = max(2, (w - box_w) // 2)

    path_key = path_to_str(CURRENT_PATH)
    idx = _MENU_CURSOR.get(path_key, 0)

    while True:
        draw_box(stdscr, top, left, box_h, box_w)
        stdscr.addstr(
            top + 1, left + 2,
            "[Arrows] Move   [Enter] Select   [F2] Back   [F3] Month View",
            curses.A_DIM
        )

        for i, txt in enumerate(items):
            attr = curses.color_pair(PALETTE["select"]) if i == idx else curses.A_NORMAL
            stdscr.addstr(top + 3 + i, left + 4, str(txt).ljust(box_w - 8), attr)

        stdscr.refresh()
        k = stdscr.getch()

        # global hotkeys (e.g. F3) handled here
        if _hotkey_handle(stdscr, k):
            init_theme(stdscr); title_bar(stdscr, path_to_str(CURRENT_PATH), title)
            continue

        if k == curses.KEY_F2:
            _MENU_CURSOR[path_key] = idx
            return -1
        elif k == curses.KEY_UP:
            idx = (idx - 1) % len(items)
        elif k == curses.KEY_DOWN:
            idx = (idx + 1) % len(items)
        elif k in (10, 13):  # Enter
            _MENU_CURSOR[path_key] = idx
            return idx



def report_low_stock_txt():
    """Create reports/low_stock/low_stock_YYYYMMDD.txt and return its path."""
    os.makedirs(REPORTS_LOW_STOCK_DIR, exist_ok=True)
    path = os.path.join(REPORTS_LOW_STOCK_DIR, f"low_stock_{datetime.date.today():%Y%m%d}.txt")

    sql = """
    SELECT i.id, i.brand, i.name, i.size, i.uom, i.upc, i.reorder_level,
           COALESCE(SUM(l.qty),0) AS stock
    FROM items i
    LEFT JOIN lots l ON l.item_id = i.id
    GROUP BY i.id
    HAVING i.reorder_level > 0 AND stock <= i.reorder_level
    ORDER BY i.brand, i.name
    """
    with get_db() as con, open(path, "w", encoding="utf-8") as f:
        f.write("LOW STOCK REPORT\n")
        f.write(f"Generated: {datetime.datetime.now():%Y-%m-%d %H:%M}\n")
        f.write("-" * 100 + "\n")
        f.write("ID  BRAND  NAME  SIZE UOM  UPC        STOCK  REORDER\n")
        for r in con.execute(sql):
            f.write(
                f"{r[0]:>4}  {(r[1] or '')}  {(r[2] or '')}  {(r[3] or '')} {(r[4] or '')}  "
                f"{(r[5] or ''):>10}   {int(r[7]) if r[7] is not None else 0:>5}   {int(r[6]) if r[6] is not None else 0:>5}\n"
            )
    return path


def report_expiries_txt(start_ymd: str, end_ymd: str):
    """Create reports/expiries/expiries_START_END.txt and return its path."""
    os.makedirs(REPORTS_EXPIRIES_DIR, exist_ok=True)
    path = os.path.join(REPORTS_EXPIRIES_DIR, f"expiries_{start_ymd}_{end_ymd}.txt")

    sql = """
    SELECT l.expiry, l.qty, i.upc, i.brand, i.name, i.size, i.uom, l.lot
    FROM lots l
    JOIN items i ON i.id = l.item_id
    WHERE l.expiry BETWEEN ? AND ?
    ORDER BY l.expiry, i.brand, i.name
    """
    with get_db() as con, open(path, "w", encoding="utf-8") as f:
        f.write(f"EXPIRIES {fmt_ymd(start_ymd)} – {fmt_ymd(end_ymd)}\n")
        f.write(f"Generated: {datetime.datetime.now():%Y-%m-%d %H:%M}\n")
        f.write("-" * 100 + "\n")
        f.write("EXPIRY      QTY  UPC        BRAND  NAME  SIZE UOM  LOT\n")
        for r in con.execute(sql, (start_ymd, end_ymd)):
            f.write(
                f"{fmt_ymd(r[0]):10} {int(r[1]) if r[1] is not None else 0:>3} "
                f"{(r[2] or ''):>10}  {(r[3] or '')}  {(r[4] or '')}  {(r[5] or '')} {(r[6] or '')}  {(r[7] or '')}\n"
            )
    return path

def report_low_stock_pdf():
    """
    Create reports/low_stock/low_stock_YYYYMMDD.pdf (ReportLab).
    Returns (path, ok_pdf=True). If ReportLab isn't available, falls back to TXT and returns (txt_path, False).
    """
    os.makedirs(REPORTS_LOW_STOCK_DIR, exist_ok=True)
    today = datetime.date.today()
    pdf_path = os.path.join(REPORTS_LOW_STOCK_DIR, f"low_stock_{today:%Y%m%d}.pdf")

    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        width, height = letter

        sql = """
        SELECT i.id, i.brand, i.name, i.size, i.uom, i.upc, i.reorder_level,
               COALESCE(SUM(l.qty),0) AS stock
        FROM items i
        LEFT JOIN lots l ON l.item_id = i.id
        GROUP BY i.id
        HAVING i.reorder_level > 0 AND stock <= i.reorder_level
        ORDER BY i.brand, i.name
        """
        with get_db() as con:
            rows = list(con.execute(sql))

        c = canvas.Canvas(pdf_path, pagesize=letter)
        y = height - 54
        c.setFont("Helvetica-Bold", 14); c.drawString(54, y, "LOW STOCK REPORT"); y -= 16
        c.setFont("Helvetica", 10); c.drawString(54, y, f"Generated: {datetime.datetime.now():%Y-%m-%d %H:%M}"); y -= 18
        c.setFont("Helvetica-Bold", 10); c.drawString(54, y, "ID  BRAND  NAME  SIZE UOM  UPC        STOCK  REORDER"); y -= 14
        c.setFont("Helvetica", 10)
        for r in rows:
            line = (
                f"{r[0]:>4}  {(r[1] or '')}  {(r[2] or '')}  "
                f"{(r[3] or '')} {(r[4] or '')}  {(r[5] or ''):>10}   "
                f"{int(r[7]) if r[7] is not None else 0:>5}   {int(r[6]) if r[6] is not None else 0:>5}"
            )
            if y < 54:
                c.showPage(); y = height - 54; c.setFont("Helvetica", 10)
            c.drawString(54, y, line[:100]); y -= 12
        c.save()
        return pdf_path, True
    except Exception:
        # graceful fallback
        return report_low_stock_txt(), False


def report_expiries_pdf(start_ymd: str, end_ymd: str):
    """
    Create reports/expiries/expiries_START_END.pdf (ReportLab).
    Returns (path, ok_pdf=True). If ReportLab isn't available, falls back to TXT and returns (txt_path, False).
    """
    os.makedirs(REPORTS_EXPIRIES_DIR, exist_ok=True)
    pdf_path = os.path.join(REPORTS_EXPIRIES_DIR, f"expiries_{start_ymd}_{end_ymd}.pdf")

    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        width, height = letter

        sql = """
        SELECT l.expiry, l.qty, i.upc, i.brand, i.name, i.size, i.uom, l.lot
        FROM lots l
        JOIN items i ON i.id = l.item_id
        WHERE l.expiry BETWEEN ? AND ?
        ORDER BY l.expiry, i.brand, i.name
        """
        with get_db() as con:
            rows = list(con.execute(sql, (start_ymd, end_ymd)))

        c = canvas.Canvas(pdf_path, pagesize=letter)
        y = height - 54
        c.setFont("Helvetica-Bold", 14); c.drawString(54, y, f"EXPIRIES {fmt_ymd(start_ymd)} – {fmt_ymd(end_ymd)}"); y -= 16
        c.setFont("Helvetica", 10); c.drawString(54, y, f"Generated: {datetime.datetime.now():%Y-%m-%d %H:%M}"); y -= 18
        c.setFont("Helvetica-Bold", 10); c.drawString(54, y, "EXPIRY      QTY  UPC        BRAND  NAME  SIZE UOM  LOT"); y -= 14
        c.setFont("Helvetica", 10)
        for r in rows:
            line = (
                f"{fmt_ymd(r[0]):10} {int(r[1]) if r[1] is not None else 0:>3} "
                f"{(r[2] or ''):>10}  {(r[3] or '')}  {(r[4] or '')}  {(r[5] or '')} {(r[6] or '')}  {(r[7] or '')}"
            )
            if y < 54:
                c.showPage(); y = height - 54; c.setFont("Helvetica", 10)
            c.drawString(54, y, line[:100]); y -= 12
        c.save()
        return pdf_path, True
    except Exception:
        # graceful fallback
        return report_expiries_txt(start_ymd, end_ymd), False


# ---- item detail screen --------------------------------------------------

def screen_item_details(stdscr, item_id: int):
    """Item Details, with [D] Delete Item."""
    push_path("PPM/QUERY/DETAILS")
    try:
        init_theme(stdscr)
        it = dao_item_get(item_id) or {}
        lots = dao_item_lots(item_id) or []

        # layout
        h, w = stdscr.getmaxyx()
        bw = min(max(64, int(w * 0.85)), w - 4)
        # leave some height to list lots
        bh = min(max(18, 20 + len(lots)), h - 4)
        top = max(3, (h - bh) // 2)
        left = max(2, (w - bw) // 2)

        draw_box(stdscr, top, left, bh, bw)

        y = top + 2
        x = left + 2

        # header / fields
        stdscr.addstr(y, x, f"#{it.get('id','')}  {(it.get('brand') or '')}  {(it.get('name') or '')}"); y += 1
        stdscr.addstr(y, x, f"UPC: {it.get('upc','') or ''}    Vendor: {it.get('vendor','') or ''}"); y += 1
        cost = it.get('cost') or 0.0
        retail = it.get('retail') or 0.0
        stdscr.addstr(y, x, f"Cost: {cost:0.2f}    Retail: {retail:0.2f}"); y += 1
        stdscr.addstr(y, x, f"Reorder Level: {it.get('reorder_level') or 0}    Current Stock: {dao_item_stock_qty(item_id)}"); y += 1
        stdscr.addstr(y, x, f"Notes: {it.get('notes') or ''}"); y += 2

        # lots
        stdscr.hline(y, x, curses.ACS_HLINE, bw - 4); y += 1
        stdscr.addstr(y, x, "Lots (by expiry)"); y += 1
        stdscr.addstr(y, x, " " + "Lot".ljust(16) + "Expiry".ljust(14) + "Qty"); y += 1

        # render as many lots as fit
        max_lines = max(0, (top + bh - 3) - y)
        for lr in lots[:max_lines]:
            exp = fmt_ymd(lr.get("expiry", ""))
            lot = (lr.get("lot") or "")[:16]
            qty = str(lr.get("qty") or 0)
            stdscr.addstr(y, x, " " + lot.ljust(16) + exp.ljust(14) + qty)
            y += 1

        # help line with Delete hotkey
        help_line = "[Arrows] Move   [Enter] Select   [D] Delete Item   [F2] Back"
        stdscr.addstr(top + bh - 2, left + 2, help_line, curses.A_DIM)
        stdscr.refresh()

        # input loop
        while True:
            k = stdscr.getch()

            if k == curses.KEY_F2:
                # back
                return

            elif k in (ord('d'), ord('D')):
                if confirm_dialog(stdscr, "DELETE ITEM", "Delete this item and all its lots?"):
                    dao_item_delete(item_id)
                    info_toast(stdscr, "Item deleted.")
                    log_action(CURRENT_USER.get("initials",""), "item_delete", f"id={item_id}")
                    return

            elif k in (10, 13):
                # Enter: nothing to do on a read-only details view; keep showing the screen
                continue

            # ignore other keys

    finally:
        pop_path()

# ---- query helpers -------------------------------------------------------

def _render_item_line(i, r, maxw):
    s = f"#{r['id']:>4}  {(r['brand'] or '')}  {(r['name'] or '')}  {(r['size'] or '')} {(r['uom'] or '')}"
    return s[:maxw]

def _render_lot_line_week(i, r, maxw):
    disp = f"{fmt_ymd(r['expiry']).ljust(12)}  {str(r['qty']).rjust(4)}  {(r['brand'] or '')}  {(r['name'] or '')}  [{r['lot'] or ''}]"
    attr = expiry_attr_for_week_view(r['expiry'])
    return (disp[:maxw], attr)

# ---- item query screens --------------------------------------------------

def screen_item_query(stdscr):
    push_path("PPM/QUERY")
    try:
        while True:
            idx = menu(stdscr, "ITEM QUERY",
           ["By Description", "By UPC/Barcode", "By Expiry (YYYYMMDD)", "All Items"])

            if idx == -1:
                return

            # By Description
            if idx == 0:
                f = input_form(stdscr, "SEARCH BY DESCRIPTION",
                               [("Keywords","")],
                               footer="[Enter] Search   [F2] Back")
                if f is None: 
                    continue
                q = (f["Keywords"] or "").strip()
                rows = dao_search_by_desc(q)
                if not rows:
                    info_center(stdscr, "RESULTS", "(no results)")
                    continue
                sel = list_paginated(stdscr, "RESULTS", rows, _render_item_line, page_size=12)
                if sel >= 0:
                    screen_item_details(stdscr, rows[sel]["id"])
                continue

            # By UPC
            if idx == 1:
                f = input_form(stdscr, "SEARCH BY UPC/BARCODE",
                               [("UPC/Fragment","")],
                               footer="[Enter] Search   [F2] Back")
                if f is None: 
                    continue
                q = (f["UPC/Fragment"] or "").strip()
                rows = dao_search_by_upc(q)
                if not rows:
                    info_center(stdscr, "RESULTS", "(no results)")
                    continue
                sel = list_paginated(stdscr, "RESULTS", rows, _render_item_line, page_size=12)
                if sel >= 0:
                    screen_item_details(stdscr, rows[sel]["id"])
                continue

            # By Expiry week (starting date)

            if idx == 2:
                f = input_form(stdscr, "EXPIRY WINDOW (WEEK)",
                               [("Start (YYYYMMDD)","")],
                               footer="[Enter] Show   [F2] Back")
                if f is None:
                    continue
                raw = (f["Start (YYYYMMDD)"] or "").strip()
                start = parse_date_input(raw)
                if not start:
                    warn_toast(stdscr, "Invalid date.")
                    continue

                rows = _query_items_by_expiry_week(start)
                if not rows:
                    info_center(stdscr, "RESULTS", "(no results)")
                    continue

                # Open Item Details when you press Enter on a row
                sel = list_paginated(stdscr, "EXPIRING (NEXT 7 DAYS)",
                                     rows, _render_lot_line_week, page_size=14)
                if sel >= 0:
                    screen_item_details(stdscr, rows[sel]["item_id"])
                continue

            # All Items
            if idx == 3:
                try:
                    rows = dao_search_by_desc("") or dao_all_items()
                except NameError:
                    rows = dao_all_items()

                if not rows:
                    info_center(stdscr, "RESULTS", "(no results)")
                    continue

                sel = list_paginated(stdscr, "RESULTS", rows, _render_item_line, page_size=12)
                if sel >= 0:
                    screen_item_details(stdscr, rows[sel]["id"])
                continue

    finally:
        pop_path()

# ---- add/update item (with optional initial lot/expiry) ------------------

def screen_add_item(stdscr):
    push_path("PPM/ADD")
    try:
        # 1) Gather fields
        fields = [
            ("Brand",""), ("Name",""), ("UPC",""),
            ("Size",""), ("UOM",""), ("Vendor",""),
            ("Cost",""), ("Retail",""),
            ("Reorder Level","0"),
            ("Lot (optional)",""),
            ("Expiry (YYYYMM or YYYYMMDD, optional)",""),
            ("Notes",""),
        ]

        while True:
            f = input_form_tab_submit(
                stdscr, "ITEM MAINTENANCE (Add/Update)",
                fields,
                footer="[Tab] Review   [Enter] Next   [F2] Back"
            )
            if f is None:
                return

            # normalize basic values for review + save
            d = dict(f)
            brand  = (d.get("Brand","") or "").strip()
            name   = (d.get("Name","") or "").strip()
            upc    = (d.get("UPC","") or "").strip()
            size   = (d.get("Size","") or "").strip()
            uom    = (d.get("UOM","") or "").strip()
            vendor = (d.get("Vendor","") or "").strip()
            try:    cost   = float((d.get("Cost","")   or "0").strip())
            except: cost   = 0.0
            try:    retail = float((d.get("Retail","") or "0").strip())
            except: retail = 0.0
            try:    reorder = int((d.get("Reorder Level","") or "0").strip())
            except: reorder = 0
            lot    = (d.get("Lot (optional)","") or "").strip()

            # compute a preview expiry for the review dialog
            exp_in = (d.get("Expiry (YYYYMM or YYYYMMDD, optional)","") or "").strip()
            preview_exp = parse_date_input(exp_in) if exp_in else ""
            if not preview_exp and lot:
                preview_exp = decode_lot_basic(lot)
            d["Expiry"] = preview_exp  # used by review dialog

            # 2) Review
            act = review_item_dialog(stdscr, d)
            if act == "back":
                return
            if act == "edit":
                # reopen the form pre-filled with the same values
                fields = [(k, d.get(k, "")) for (k, _) in fields]
                continue

            # 3) Save (act == "save")
            typed_explicit = bool(exp_in)
            expiry = parse_date_input(exp_in) if exp_in else ""
            if not expiry and lot:
                expiry = decode_lot_basic(lot)

            # If the user typed a date but it's invalid, stop and let them fix it
            if typed_explicit and not expiry:
                warn_toast(stdscr, "Invalid expiry. Use YYYYMM or YYYYMMDD.")
                # show the form again with current values
                fields = [(k, d.get(k, "")) for (k, _) in fields]
                continue

            if not (brand or name or upc):
                warn_toast(stdscr, "Brand, Name or UPC required to save.")
                fields = [(k, d.get(k, "")) for (k, _) in fields]
                continue

            # Upsert item header (support either DAO name your file uses)
            _upsert = (
                globals().get("dao_item_upsert")
                or globals().get("get_or_create_item")
                or globals().get("add_or_create_item")
            )
            if not _upsert:
                warn_toast(stdscr, "Internal error: missing upsert function.")
                return

            item_id = _upsert(brand, name, upc, size, uom, vendor, cost, retail, d.get("Notes",""), reorder)

            # Make a lot row if either field was provided
            if lot or expiry:
                add_lot(item_id, lot, expiry, qty=0)

            info_toast(stdscr, f"Saved item #{item_id}.")
            log_action(CURRENT_USER.get("initials",""), "item_save", f"id={item_id}")
            return
    finally:
        pop_path()

# --- Picklist API shims (UI name -> DAO name) ------------------------------
# Paste this near your DAO compatibility functions, ABOVE screen_picklists()

if "dao_picklist_create" in globals() and "picklist_create" not in globals():
    def picklist_create(name: str):
        return dao_picklist_create(name)

if "dao_picklists_all" in globals() and "picklists_all" not in globals():
    def picklists_all():
        return dao_picklists_all()

if "dao_picklist_delete" in globals() and "picklist_delete" not in globals():
    def picklist_delete(pid: int):
        return dao_picklist_delete(pid)

if "dao_picklist_toggle" in globals() and "picklist_toggle" not in globals():
    def picklist_toggle(pid: int):
        return dao_picklist_toggle(pid)


# ---- regular expiry entry ------------------------------------------------
# ----- shared: pick an item by quick search (self-contained) -----
def pick_item_by_search(stdscr, title="SELECT ITEM"):
    """
    1) Ask for keywords (brand/name/notes)
    2) Show a paged list of matching items
    3) Return selected item_id or None
    """
    push_path("PPM/PICK")
    try:
        # 1) prompt
        f = input_form(stdscr, f"{title} — SEARCH",
                       [("Keywords", "")],
                       footer="[Enter] Search   [F2] Back")
        if f is None:
            return None
        q = (f["Keywords"] or "").strip()
        if not q:
            info_center(stdscr, "RESULTS", "(enter a keyword)")
            return None

        like = f"%{q}%"
        # 2) fetch matches directly (kept self-contained so it doesn't depend on other helpers)
        with db() as con:
            rows = con.execute(
                """SELECT id, brand, name, size, uom, upc
                   FROM items
                   WHERE (IFNULL(brand,'') LIKE ?
                          OR IFNULL(name,'') LIKE ?
                          OR IFNULL(notes,'') LIKE ?)
                   ORDER BY brand, name, id""",
                (like, like, like)
            ).fetchall()
        matches = [dict(r) for r in rows]
        if not matches:
            info_center(stdscr, "RESULTS", "(no results)")
            return None

        # 3) list + select
        def _line(_i, r, maxw):
            s = f"#{r['id']:>5}  {r.get('brand','') or ''}  {r.get('name','') or ''}  {r.get('size','') or ''} {r.get('uom','') or ''}"
            return s[:maxw]

        sel = list_paginated(stdscr, "MATCHING ITEMS", matches, _line, page_size=12)
        if sel < 0:
            return None
        return matches[sel]["id"]
    finally:
        pop_path()

def screen_regular_expiry(stdscr):
    push_path("PPM/REGULAR-EXPIRY")
    try:
        item_id = pick_item_by_search(stdscr, "REGULAR EXPIRY")
        if not item_id:
            return
        it = dao_item_get(item_id)

        fields = [
            ("Expiry (YYYYMM or YYYYMMDD)",""),
            ("Lot (optional)",""),
            ("Quantity",""),
        ]
        f = input_form(stdscr, f"ADD EXPIRY — #{it['id']} {it['brand']} {it['name']}",
                       fields,
                       footer="[Enter] Save   [F2] Back")
        if f is None:
            return

        raw_exp = (f["Expiry (YYYYMM or YYYYMMDD)"] or "").strip()
        lot     = (f["Lot (optional)"] or "").strip()
        qty_s   = (f["Quantity"] or "").strip()

        expiry = parse_date_input(raw_exp)
        if not expiry and lot:
            expiry = decode_lot_basic(lot)
        if not expiry:
            warn_toast(stdscr, "Invalid expiry.")
            return

        try:
            qty = int(qty_s)
        except:
            warn_toast(stdscr, "Quantity must be a number.")
            return

        dao_add_lot(item_id, lot, expiry, qty)
        log_action(CURRENT_USER.get("initials",""), "expiry_add", f"id={item_id};exp={expiry};qty={qty}")
        info_toast(stdscr, f"Added lot: {fmt_ymd(expiry)} x{qty}")
    finally:
        pop_path()

# ---- interactive decode (lot -> choose date -> qty) ---------------------

def screen_decode(stdscr):
    push_path("PPM/DECODE")
    try:
        item_id = pick_item_by_search(stdscr, "INTERACTIVE DECODE")
        if not item_id:
            return
        it = dao_item_get(item_id)

        # Step 1: enter lot
        f = input_form(stdscr, f"DECODE LOT — #{it['id']} {it['brand']} {it['name']}",
                       [("Lot","")],
                       footer="[Enter] Decode   [F2] Back")
        if f is None:
            return
        lot = (f["Lot"] or "").strip()
        if not lot:
            warn_toast(stdscr, "Lot is required.")
            return

        # Step 2: candidates
        cand = []
        exp = decode_lot_basic(lot)
        if exp:
            cand.append(exp)
        if not exp and lot.isdigit() and len(lot) in (6,8):
            exp2 = parse_date_input(lot)
            if exp2:
                cand.append(exp2)

        options = [f"Use decoded date: {fmt_ymd(c)}" for c in cand] + ["Enter date manually"]
        idx = menu(stdscr, "CHOOSE EXPIRY", options)
        if idx < 0:
            return
        if idx == len(options) - 1:
            mf = input_form(stdscr, "MANUAL EXPIRY",
                            [("Expiry (YYYYMM or YYYYMMDD)","")],
                            footer="[Enter] OK   [F2] Back")
            if mf is None:
                return
            expiry = parse_date_input((mf["Expiry (YYYYMM or YYYYMMDD)"] or "").strip())
        else:
            expiry = cand[idx]

        if not expiry:
            warn_toast(stdscr, "Invalid expiry.")
            return

        # Step 3: quantity
        qf = input_form(stdscr, f"SET QUANTITY — {fmt_ymd(expiry)}",
                        [("Quantity","")],
                        footer="[Enter] Save   [F2] Back")
        if qf is None:
            return
        try:
            qty = int((qf["Quantity"] or "").strip())
        except:
            warn_toast(stdscr, "Quantity must be a number.")
            return

        dao_add_lot(item_id, lot, expiry, qty)
        log_action(CURRENT_USER.get("initials",""), "decode_save", f"id={item_id};exp={expiry};qty={qty}")
        info_toast(stdscr, f"Saved: {fmt_ymd(expiry)} x{qty}")
    finally:
        pop_path()

# ---- PPM submenu (overrides) --------------------------------------------

def sub_ppm(stdscr):
    push_path("MAIN/PPM")
    try:
        while True:
            choice = menu(stdscr, "PRODUCT AND PRICE MANAGEMENT", [
                "ITEM QUERY",
                "ITEM MAINTENANCE (Add/Update)",
                "REGULAR EXPIRY (Printed date)",
                "INTERACTIVE DECODE (Lot→Date)"
            ])
            if choice == -1: 
                return
            if choice == 0:
                screen_item_query(stdscr)
            elif choice == 1:
                screen_add_item(stdscr)
            elif choice == 2:
                screen_regular_expiry(stdscr)
            elif choice == 3:
                screen_decode(stdscr)
    finally:
        pop_path()

# ==== ADMIN submenu =========================================================
def sub_admin(stdscr):
    push_path("MAIN/ADMIN")
    try:
        while True:
            idx = menu(stdscr, "ADMIN", [
                "Users (add/edit/delete, toggle admin, reset password)",
                "Audit log (view / delete / purge)",
            ])
            if idx == -1:
                return
            if idx == 0:
                screen_users(stdscr)
            elif idx == 1:
                screen_audit_log(stdscr)
    finally:
        pop_path()

# ---- Users screen ----------------------------------------------------------
def _render_user_row(i, r, w):
    role = "admin" if r.get("is_admin") else "user"
    return f"{r['id']:>4}  {r['initials']:<6}  {role:<5}"

def _admins_count():
    with get_db() as con:
        return con.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]

def screen_users(stdscr):
    push_path("MAIN/ADMIN/USERS")
    try:
        while True:
            rows = dao_user_list()
            init_theme(stdscr)
            title_bar(stdscr, path_to_str(CURRENT_PATH), "USERS")
            stdscr.addstr(3, 2,
                "[Enter] Edit  [N] New  [T] Toggle admin  [P] Reset PW  [D] Delete  [F2] Back",
                curses.A_DIM)
            stdscr.refresh()

            if not rows:
                # Empty state: allow create or back
                k = stdscr.getch()
                if k in (curses.KEY_F2, 27):
                    return
                if k in (ord('n'), ord('N')):
                    _create_user_dialog(stdscr)
                continue

            sel = list_paginated(stdscr, "USERS", rows, _render_user_row, page_size=12)
            if sel == -1:
                return

            u = rows[sel]
            k = stdscr.getch()
            if k in (curses.KEY_F2, 27):
                return
            elif k in (10, 13):                                  # Enter -> edit
                _edit_user_dialog(stdscr, u["id"])
            elif k in (ord('n'), ord('N')):                      # New
                _create_user_dialog(stdscr)
            elif k in (ord('t'), ord('T')):                      # Toggle admin
                admins = _admins_count()
                make_admin = not bool(u["is_admin"])
                if not make_admin and u["is_admin"] and admins <= 1:
                    warn_toast(stdscr, "Cannot remove the last admin.")
                else:
                    dao_user_update(u["id"], is_admin=make_admin)
                    info_toast(stdscr, f"{u['initials']}: admin -> {make_admin}")
            elif k in (ord('p'), ord('P')):                      # Reset PW
                _reset_pw_dialog(stdscr, u["id"], u["initials"])
            elif k in (ord('d'), ord('D')):                      # Delete
                admins = _admins_count()
                if u["is_admin"] and admins <= 1:
                    warn_toast(stdscr, "Cannot delete the last admin.")
                else:
                    if confirm_dialog(stdscr, "Delete user", f"Delete {u['initials']}?"):
                        dao_user_delete(u["id"])
                        info_toast(stdscr, "User deleted.")
    finally:
        pop_path()

def _create_user_dialog(stdscr):
    f = input_form(stdscr, "NEW USER",
                   [("Initials",""), ("Password",""), ("Admin? (Y/N)","N")],
                   footer="[Enter] Create  [F2] Cancel")
    if f is None: return
    init_ = (f.get("Initials","") or "").strip().upper()
    pw    = f.get("Password","") or ""
    adm   = (f.get("Admin? (Y/N)","N") or "N").strip().upper().startswith("Y")
    if not init_ or not pw:
        warn_toast(stdscr, "Initials and password required.")
        return
    if dao_user_get_by_initials(init_):
        warn_toast(stdscr, "Initials already exist.")
        return
    dao_user_create(init_, pw, adm)
    info_toast(stdscr, "User created.")

def _edit_user_dialog(stdscr, user_id:int):
    u = dao_user_get(user_id)
    if not u: return
    f = input_form(stdscr, "EDIT USER",
                   [("Initials", u["initials"]), ("Admin? (Y/N)", "Y" if u["is_admin"] else "N")],
                   footer="[Enter] Save  [F2] Cancel")
    if f is None: return
    init_ = (f.get("Initials","") or "").strip().upper()
    adm   = (f.get("Admin? (Y/N)","N") or "N").strip().upper().startswith("Y")
    if not init_:
        warn_toast(stdscr, "Initials required.")
        return
    existing = dao_user_get_by_initials(init_)
    if existing and existing["id"] != user_id:
        warn_toast(stdscr, "Initials already in use.")
        return
    # Prevent removing the last admin
    if u["is_admin"] and not adm and _admins_count() <= 1:
        warn_toast(stdscr, "Cannot remove the last admin.")
        return
    dao_user_update(user_id, initials=init_, is_admin=adm)
    info_toast(stdscr, "Updated.")

def _reset_pw_dialog(stdscr, user_id:int, initials:str):
    f = input_form(stdscr, f"RESET PASSWORD for {initials}", [("New Password","")],
                   footer="[Enter] Save  [F2] Cancel")
    if f is None: return
    pw = f.get("New Password","") or ""
    if not pw:
        warn_toast(stdscr, "Password required.")
        return
    dao_user_set_password(user_id, pw)
    info_toast(stdscr, "Password reset.")


def dao_remove_qty_from_lot(item_id: int, lot: str | None, qty: int) -> int:
    """
    Decrease qty for (item_id, lot) by qty; delete the lot row if it reaches 0.
    Returns the new quantity (0 if deleted/not found).
    """
    lot_key = (lot or "")
    with get_db() as con:
        row = con.execute(
            "SELECT qty FROM lots WHERE item_id=? AND IFNULL(lot,'')=?",
            (item_id, lot_key)
        ).fetchone()
        if not row:
            return 0
        cur = int(row["qty"] or 0)
        new_qty = max(0, cur - int(qty))
        if new_qty <= 0:
            con.execute("DELETE FROM lots WHERE item_id=? AND IFNULL(lot,'')=?", (item_id, lot_key))
        else:
            con.execute(
                "UPDATE lots SET qty=? WHERE item_id=? AND IFNULL(lot,'')=?",
                (new_qty, item_id, lot_key)
            )
        return new_qty

# ============================================================
# PART 6/6 — Login, main loop, entrypoint + export folders
# ============================================================

def ensure_picklist_items_table():
    """Creates/repairs picklist_items so UPSERT works and qty column exists."""
    with db() as con:
        # base table
        con.executescript("""
        CREATE TABLE IF NOT EXISTS picklist_items(
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            picklist_id  INTEGER NOT NULL REFERENCES picklists(id) ON DELETE CASCADE,
            item_id      INTEGER NOT NULL REFERENCES items(id)     ON DELETE CASCADE,
            qty          INTEGER NOT NULL DEFAULT 0
        );
        """)

        # make sure qty column exists (older tables may miss it)
        cols = [r["name"] for r in con.execute("PRAGMA table_info(picklist_items)").fetchall()]
        if "qty" not in cols:
            con.execute("ALTER TABLE picklist_items ADD COLUMN qty INTEGER NOT NULL DEFAULT 0")

        # ensure UNIQUE constraint for (picklist_id,item_id) so ON CONFLICT works
        con.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_picklist_items_pick_item
            ON picklist_items(picklist_id, item_id)
        """)

# --- DAO helpers for picklist header + line items --------------------------
def dao_picklist_get(pid: int):
    with db() as con:
        r = con.execute(
            "SELECT id, name, status, created_at, created_by FROM picklists WHERE id=?",
            (pid,)
        ).fetchone()
    return dict(r) if r else None

def dao_picklist_items_list(pid: int):
    """
    Returns line items for a picklist. Works whether the table has
    'qty' or an older 'quantity' column (or neither).
    """
    with db() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(picklist_items)")}
        qty_col = "li.qty" if "qty" in cols else ("li.quantity" if "quantity" in cols else "1")
        sql = f"""
            SELECT li.id, li.item_id, {qty_col} AS qty,
                   i.brand, i.name, i.size, i.uom
            FROM picklist_items li
            JOIN items i ON i.id = li.item_id
            WHERE li.picklist_id = ?
            ORDER BY i.brand, i.name, i.id
        """
        rows = con.execute(sql, (pid,)).fetchall()
    return [dict(r) for r in rows]


# --- export folders (printables land here) ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
EXPORT_DIR = os.path.join(BASE_DIR, "exports")
EXPORT_PICKLISTS_DIR = os.path.join(EXPORT_DIR, "picklists")
EXPORT_LOW_DIR       = os.path.join(EXPORT_DIR, "low_stock")
EXPORT_EXP_DIR       = os.path.join(EXPORT_DIR, "expiries")

def ensure_export_dirs():
    for p in (EXPORT_DIR, EXPORT_PICKLISTS_DIR, EXPORT_LOW_DIR, EXPORT_EXP_DIR):
        try:
            os.makedirs(p, exist_ok=True)
        except Exception:
            pass

# ---- LOGIN -------------------------------------------------

def login_screen(stdscr):
    """
    Flow:
      - User / Password (password masked)
      - Enter moves from User -> Password; Enter on Password submits
      - F2 asks to exit program
    Returns True on successful sign-in, False if user chose to exit program.
    """
    CURRENT_PATH[:] = ["MAIN"]
    while True:
        init_theme(stdscr)
        title_bar(stdscr, path_to_str(CURRENT_PATH), "SIGN IN")

        f = input_form(stdscr, "SIGN IN",
                       [("User",""), ("Password","")],
                       footer="[Enter] Sign In   [F2] Exit",
                       mask={"Password"})
        if f is None:
            if confirm_dialog(stdscr, "Exit", "Are you sure you want to close?"):
                return False
            else:
                continue

        user = (f.get("User","") or "").strip().upper()
        pwd  = (f.get("Password","") or "").strip()

        if not user or not pwd:
            warn_toast(stdscr, "Enter both user and password.")
            continue

        with db() as con:
            row = con.execute(
                "SELECT initials,password,is_admin FROM users WHERE initials=?",
                (user,)
            ).fetchone()

        if row and row["password"] == pwd:
            CURRENT_USER["initials"]  = row["initials"]
            CURRENT_USER["is_admin"]  = bool(row["is_admin"])
            log_action(CURRENT_USER["initials"], "sign_in", "")
            info_toast(stdscr, f"Welcome {CURRENT_USER['initials']}.")
            return True
        else:
            warn_toast(stdscr, "Invalid user/password. Try again.")

# ---- MAIN MENU --------------------------------------------

# --- PDT submenu --------------------------------------------------------------
def sub_pdt(stdscr):
    """PDT procedures: currently just Pick Lists."""
    push_path("MAIN/PDT")
    try:
        while True:
            choice = menu(stdscr, "PDT PROCEDURES", [
                "PICK LISTS",
            ])
            if choice == -1:
                return
            if choice == 0:
                if "screen_picklists" in globals():
                    screen_picklists(stdscr)
                else:
                    info_center(stdscr, "PICK LISTS", "screen_picklists() not found in this build.")
    finally:
        pop_path()

def sub_reports(stdscr):
    push_path("REPORTS")
    try:
        while True:
            choice = menu(stdscr, "REPORTS", [
                "Low Stock (TXT -> reports/low_stock)",
                "Low Stock (PDF -> reports/low_stock)",
                "Nearing Expiries (choose range) (TXT -> reports/expiries)",
                "Nearing Expiries (choose range) (PDF -> reports/expiries)",
                "Back",
            ])
            if choice in (-1, 4):
                return

            if choice == 0:
                path = report_low_stock_txt()
                info_toast(stdscr, f"Saved TXT: {os.path.basename(path)}")

            elif choice == 1:
                path, ok_pdf = report_low_stock_pdf()
                info_toast(stdscr, f"Saved {'PDF' if ok_pdf else 'TXT (no PDF engine)'}: {os.path.basename(path)}")

            elif choice == 2:
                start_ymd, end_ymd = prompt_expiry_range(stdscr)
                if not start_ymd:
                    continue
                path = report_expiries_txt(start_ymd, end_ymd)
                info_toast(stdscr, f"Saved TXT: {os.path.basename(path)}")

            elif choice == 3:
                start_ymd, end_ymd = prompt_expiry_range(stdscr)
                if not start_ymd:
                    continue
                path, ok_pdf = report_expiries_pdf(start_ymd, end_ymd)
                info_toast(stdscr, f"Saved {'PDF' if ok_pdf else 'TXT (no PDF engine)'}: {os.path.basename(path)}")
    finally:
        pop_path()

choices = [
    "PRODUCT AND PRICE MANAGEMENT",
    "PICK LISTS",
    "REPORTS",
    # (maybe other entries you already have)
]
if CURRENT_USER.get("is_admin"):
    choices.append("ADMIN")
choices.append("LOG OUT")

def main_menu(stdscr):
    while True:
        CURRENT_PATH[:] = ["MAIN"]
        choice = menu(stdscr, "MAIN MENU", [
            "PRODUCT AND PRICE MANAGEMENT",
            "PDT PROCEDURES",
            "REPORTS",
            "ADMIN",
            "LOG OUT"
        ])
        if choice == -1:
            if confirm_dialog(stdscr, "Exit", "Are you sure you want to close?"):
                return  # exit program
            else:
                continue

        if choice == 0:
            sub_ppm(stdscr)
        elif choice == 1:
            sub_pdt(stdscr)
        elif choice == 2:
            sub_reports(stdscr)
        if choice == 0:
            sub_ppm(stdscr)
        elif choice == 1:
            sub_pdt(stdscr)
        elif choice == 2:
            sub_reports(stdscr)
        elif choice == 3:
            # ADMIN: require override if current user isn't admin
            if not CURRENT_USER.get("is_admin"):
                if not admin_gate(stdscr, "Open ADMIN"):
                    continue
            try:
                sub_admin(stdscr)
            finally:
                clear_admin_override()
        elif choice == 4:
            # log out to login screen
            return "LOGOUT"

def auth_login(stdscr) -> bool:
    """Login screen. Returns True on success, False on cancel/quit."""
    push_path("LOGIN")
    try:
        ensure_users_table()
        while True:
            nf = input_form(
                stdscr, "LOGIN",
                [("Initials",""), ("Password","")],
                footer="[Enter] Log in   [F2] Quit"
            )
            if nf is None:  # F2
                return False

            initials = (nf.get("Initials") or "").strip()
            pw       = nf.get("Password") or ""
            u = dao_user_get_by_initials(initials)
            if u and u.get("pw_hash") == hash_pw(pw):
                globals()["CURRENT_USER"] = {
                    "id": u["id"],
                    "initials": u["initials"],
                    "is_admin": bool(u["is_admin"]),
                }
                log_action(u["initials"], "login", "")
                return True

            info_center(stdscr, "LOGIN FAILED", "Check initials/password and try again.")
    finally:
        pop_path()


# ---- RUN LOOP ---------------------------------------------

def run_app(stdscr):
    curses.curs_set(0)
    init_theme(stdscr)
    _ensure_extra_pairs()  # make sure color pairs for red/yellow/green exist

    while True:
        ok = login_screen(stdscr)
        if not ok:
            return  # closed at login

        while True:
            res = main_menu(stdscr)
            if res == "LOGOUT":
                CURRENT_USER["initials"] = ""
                CURRENT_USER["is_admin"] = False
                break  # back to login
            elif res is None:
                return  # user confirmed exit
            else:
                pass

# ---- ENTRYPOINT -------------------------------------------

# --- DB schema: picklists header table (open/closed) ---
def ensure_picklists_table():
    """Create the picklists header table. Safe to call multiple times."""
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS picklists(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'open',  -- open|closed
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL
        );
        """)

def run():
    init_db()
    ensure_picklists_table()       # basic picklist headers
    ensure_picklist_items_table()  # line-items per picklist
    ensure_export_dirs()           # exports/picklists, exports/low_stock, exports/expiries
    curses.wrapper(run_app)

if __name__ == "__main__":
    run()
