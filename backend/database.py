"""SQLite storage for CardVault. One file, no server process needed.

Schema has profiles (one per person) and folders (one level, per profile,
for sorting e.g. Sports Cards vs TCG). Existing installs get migrated in
place -- see init_db() -- so upgrading doesn't lose anything already
scraped in.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# defaults to the same local file it's always used -- existing installs are
# unaffected. Only set CARDVAULT_DB_PATH if you need it elsewhere (e.g. a
# mounted volume in a container).
DB_PATH = Path(os.environ.get("CARDVAULT_DB_PATH", str(Path(__file__).parent / "cardvault.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

PROFILES_AND_FOLDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    password_salt TEXT,
    password_hash TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(profile_id, name)
);
"""

CARDS_SCHEMA = """
-- `pricecharting_id`/`product_url` are named for the pricing source this
-- schema was originally built against. Left as-is rather than renamed --
-- kept working for eBay's data unchanged (item_id / listing url), and
-- renaming would be a migration for zero functional gain. See
-- backend/ebay_api.py for what actually populates them now.
CREATE TABLE cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    pricecharting_id TEXT,
    title TEXT NOT NULL,
    set_name TEXT,
    category TEXT,
    image_url TEXT,
    product_url TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    graded_as TEXT NOT NULL DEFAULT 'Ungraded',
    status TEXT NOT NULL DEFAULT 'owned',
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(profile_id, pricecharting_id)
);

CREATE TABLE IF NOT EXISTS current_prices (
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    grade TEXT NOT NULL,
    price REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (card_id, grade)
);
"""

DEFAULT_PROFILE_NAME = "My Collection"


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_default_profile(conn) -> int:
    row = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO profiles (name, is_admin) VALUES (?, 1)", (DEFAULT_PROFILE_NAME,)
    )
    return cur.lastrowid


def _migrate_profiles_table(conn):
    """Existing installs (from the pre-auth version) have a profiles table
    without password/admin columns. Add them, then make sure exactly one
    admin exists so nobody gets locked out of profile management."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(profiles)")}
    if "is_admin" not in cols:
        print("[db] adding password/admin columns to profiles...")
        conn.execute("ALTER TABLE profiles ADD COLUMN password_salt TEXT")
        conn.execute("ALTER TABLE profiles ADD COLUMN password_hash TEXT")
        conn.execute("ALTER TABLE profiles ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

    has_admin = conn.execute("SELECT 1 FROM profiles WHERE is_admin = 1 LIMIT 1").fetchone()
    if not has_admin:
        first = conn.execute("SELECT id, name FROM profiles ORDER BY id LIMIT 1").fetchone()
        if first:
            conn.execute("UPDATE profiles SET is_admin = 1 WHERE id = ?", (first["id"],))
            print(f"[db] made {first['name']!r} the admin profile (no password set yet -- "
                  f"the first password entered for it at login becomes its password)")


def init_db():
    with get_db() as conn:
        conn.executescript(PROFILES_AND_FOLDERS_SCHEMA)
        _migrate_profiles_table(conn)

        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cards'"
        ).fetchone()

        if table_exists is None:
            conn.executescript(CARDS_SCHEMA)
            _ensure_default_profile(conn)
            return

        cols = {row["name"] for row in conn.execute("PRAGMA table_info(cards)")}
        if "profile_id" in cols:
            if "graded_as" not in cols:
                print("[db] adding graded_as column to cards...")
                conn.execute("ALTER TABLE cards ADD COLUMN graded_as TEXT NOT NULL DEFAULT 'Ungraded'")
            if "status" not in cols:
                print("[db] adding status column to cards (existing cards default to 'owned')...")
                conn.execute("ALTER TABLE cards ADD COLUMN status TEXT NOT NULL DEFAULT 'owned'")
            return  # already on the current schema

        # pre-profile install: rebuild the table and carry existing rows into
        # a default profile so nothing already scraped gets lost.
        # foreign_keys is turned off for this block on purpose: SQLite
        # auto-rewrites current_prices' FK to point at cards_old the moment
        # we rename cards -> cards_old, and with FKs enforced, dropping
        # cards_old afterward cascades straight through and wipes every
        # price row. Confirmed this the hard way against a real copy of the
        # old schema before shipping it.
        print("[db] migrating cards table to add profiles/folders support...")
        conn.execute("PRAGMA foreign_keys = OFF")
        default_profile_id = _ensure_default_profile(conn)
        conn.execute("ALTER TABLE cards RENAME TO cards_old")
        conn.executescript(CARDS_SCHEMA)
        conn.execute(
            """
            INSERT INTO cards (id, profile_id, folder_id, pricecharting_id, title,
                                set_name, category, image_url, product_url, quantity, added_at)
            SELECT id, ?, NULL, pricecharting_id, title, set_name, category,
                   image_url, product_url, quantity, added_at
            FROM cards_old
            """,
            (default_profile_id,),
        )
        conn.execute("DROP TABLE cards_old")
        conn.execute("PRAGMA foreign_keys = ON")
        print(f"[db] migration complete -- existing cards assigned to profile "
              f"{DEFAULT_PROFILE_NAME!r} (id={default_profile_id})")


# ---- profiles ----

def list_profiles_public(conn):
    """Safe to expose pre-login (for the login screen's account list) --
    never includes password_salt/password_hash."""
    return [
        dict(r) for r in conn.execute(
            "SELECT id, name, is_admin, (password_hash IS NOT NULL) AS has_password, "
            "created_at FROM profiles ORDER BY name"
        ).fetchall()
    ]


def get_profile(conn, profile_id: int):
    row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return dict(row) if row else None


def get_profile_by_username(conn, username: str):
    row = conn.execute(
        "SELECT * FROM profiles WHERE lower(name) = lower(?)", (username.strip(),)
    ).fetchone()
    return dict(row) if row else None


def rename_profile(conn, profile_id: int, new_username: str):
    conn.execute(
        "UPDATE profiles SET name = ? WHERE id = ?", (new_username.strip(), profile_id)
    )


def create_profile(conn, name: str, is_admin: bool = False) -> int:
    cur = conn.execute(
        "INSERT INTO profiles (name, is_admin) VALUES (?, ?)", (name, int(is_admin))
    )
    return cur.lastrowid


def set_password(conn, profile_id: int, salt: str, password_hash: str):
    conn.execute(
        "UPDATE profiles SET password_salt = ?, password_hash = ? WHERE id = ?",
        (salt, password_hash, profile_id),
    )


def delete_profile(conn, profile_id: int):
    conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))


def admin_count(conn) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM profiles WHERE is_admin = 1").fetchone()
    return row["n"]


# ---- sessions ----

def create_session(conn, token: str, profile_id: int, expires_at: str):
    conn.execute(
        "INSERT INTO sessions (token, profile_id, expires_at) VALUES (?, ?, ?)",
        (token, profile_id, expires_at),
    )


def get_session_profile(conn, token: str, now_iso: str):
    return conn.execute(
        """
        SELECT profiles.id, profiles.name, profiles.is_admin
        FROM sessions JOIN profiles ON profiles.id = sessions.profile_id
        WHERE sessions.token = ? AND sessions.expires_at > ?
        """,
        (token, now_iso),
    ).fetchone()


def delete_session(conn, token: str):
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ---- folders ----

def list_folders(conn, profile_id: int):
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM folders WHERE profile_id = ? ORDER BY name", (profile_id,)
        ).fetchall()
    ]


def create_folder(conn, profile_id: int, name: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO folders (profile_id, name) VALUES (?, ?)",
        (profile_id, name),
    )
    row = conn.execute(
        "SELECT id FROM folders WHERE profile_id = ? AND name = ?", (profile_id, name)
    ).fetchone()
    return row["id"]


def delete_folder(conn, profile_id: int, folder_id: int):
    # cards.folder_id is ON DELETE SET NULL, so cards in this folder become
    # unfiled ("All Cards") rather than being deleted along with it
    conn.execute("DELETE FROM folders WHERE id = ? AND profile_id = ?", (folder_id, profile_id))


def move_cards(conn, profile_id: int, card_ids: list[int], folder_id: int | None):
    placeholders = ",".join("?" for _ in card_ids)
    conn.execute(
        f"UPDATE cards SET folder_id = ? WHERE profile_id = ? AND id IN ({placeholders})",
        (folder_id, profile_id, *card_ids),
    )


# ---- cards ----

def upsert_card(conn, profile_id, pricecharting_id, title, set_name, category,
                 image_url, product_url, quantity, folder_id=None, status="owned"):
    cur = conn.execute(
        "SELECT id FROM cards WHERE profile_id = ? AND pricecharting_id = ?",
        (profile_id, pricecharting_id),
    )
    row = cur.fetchone()
    if row:
        conn.execute(
            "UPDATE cards SET title=?, set_name=?, category=?, image_url=?, "
            "product_url=?, quantity=quantity+?, status=? WHERE id=?",
            (title, set_name, category, image_url, product_url, quantity, status, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO cards (profile_id, folder_id, pricecharting_id, title, set_name, "
        "category, image_url, product_url, quantity, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (profile_id, folder_id, pricecharting_id, title, set_name, category,
         image_url, product_url, quantity, status),
    )
    return cur.lastrowid


def update_card(conn, profile_id: int, card_id: int, quantity: int, graded_as: str):
    conn.execute(
        "UPDATE cards SET quantity = ?, graded_as = ? WHERE id = ? AND profile_id = ?",
        (quantity, graded_as, card_id, profile_id),
    )


def set_prices(conn, card_id, prices: dict, fetched_at: str):
    for grade, price in prices.items():
        conn.execute(
            "INSERT INTO current_prices (card_id, grade, price, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(card_id, grade) DO UPDATE SET price=excluded.price, "
            "updated_at=excluded.updated_at",
            (card_id, grade, price, fetched_at),
        )


def list_cards(conn, profile_id: int, status: str = "owned"):
    cards = conn.execute(
        """
        SELECT cards.*, folders.name AS folder_name
        FROM cards LEFT JOIN folders ON folders.id = cards.folder_id
        WHERE cards.profile_id = ? AND cards.status = ?
        ORDER BY cards.title
        """,
        (profile_id, status),
    ).fetchall()
    result = []
    for card in cards:
        prices = conn.execute(
            "SELECT grade, price, updated_at FROM current_prices WHERE card_id = ?",
            (card["id"],),
        ).fetchall()
        result.append({
            **dict(card),
            "prices": {p["grade"]: p["price"] for p in prices},
            "last_updated": max((p["updated_at"] for p in prices), default=None),
        })
    return result


def delete_card(conn, profile_id: int, card_id: int):
    conn.execute("DELETE FROM cards WHERE id = ? AND profile_id = ?", (card_id, profile_id))


def set_card_status(conn, profile_id: int, card_id: int, status: str):
    conn.execute(
        "UPDATE cards SET status = ? WHERE id = ? AND profile_id = ?",
        (status, card_id, profile_id),
    )
