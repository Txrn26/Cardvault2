import asyncio
import csv
import io
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import database as db
import ebay_api

app = FastAPI(title="CardVault")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    allow_credentials=True,
)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
SESSION_COOKIE = "session"
SESSION_LIFETIME_DAYS = 30

# Only set this to true once the app is actually served over HTTPS (direct
# TLS, or behind a reverse proxy that terminates it) -- a Secure cookie is
# simply dropped by browsers over plain http://, which would silently break
# login on a LAN-only/no-HTTPS deployment.
COOKIE_SECURE = os.environ.get("CARDVAULT_COOKIE_SECURE", "false").lower() == "true"

# Basic brute-force guard on login, since this may now be reachable off a
# trusted LAN. In-memory/per-process is enough here -- it resets on
# restart/redeploy, which is an acceptable trade for not adding a
# dependency (Redis etc.) just for this.
LOGIN_RATE_LIMIT = int(os.environ.get("CARDVAULT_LOGIN_RATE_LIMIT", "10"))  # attempts
LOGIN_RATE_WINDOW_SECONDS = 60
_login_attempts: dict[str, deque] = defaultdict(deque)


def _check_login_rate_limit(client_ip: str):
    now = time.time()
    attempts = _login_attempts[client_ip]
    while attempts and now - attempts[0] > LOGIN_RATE_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= LOGIN_RATE_LIMIT:
        raise HTTPException(429, "Too many login attempts -- wait a minute and try again")
    attempts.append(now)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---- auth dependency ----

def current_session(session: str | None = Cookie(default=None)) -> dict:
    if not session:
        raise HTTPException(401, "Not logged in")
    with db.get_db() as conn:
        row = db.get_session_profile(conn, session, now_iso())
    if not row:
        raise HTTPException(401, "Session expired or invalid -- please log in again")
    return {"profile_id": row["id"], "name": row["name"], "is_admin": bool(row["is_admin"])}


def check_access(profile_id: int, sess: dict):
    """Admins can act on any profile. Everyone else only their own."""
    if not sess["is_admin"] and sess["profile_id"] != profile_id:
        raise HTTPException(403, "Not authorized for that profile")


def require_admin(sess: dict):
    if not sess["is_admin"]:
        raise HTTPException(403, "Admin rights required")


# ---- request models ----

class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class CreateProfileRequest(BaseModel):
    name: str
    is_admin: bool = False


class SetAdminRequest(BaseModel):
    is_admin: bool


class ResetPasswordRequest(BaseModel):
    new_password: str


class RenameProfileRequest(BaseModel):
    new_username: str


class CreateFolderRequest(BaseModel):
    profile_id: int
    name: str


class MoveCardsRequest(BaseModel):
    profile_id: int
    card_ids: list[int]
    folder_id: int | None = None  # null moves back to "no folder"


class AddCardRequest(BaseModel):
    item_id: str
    quantity: int = 1
    profile_id: int
    folder_id: int | None = None
    status: str = "owned"


class UpdateCardRequest(BaseModel):
    profile_id: int
    quantity: int
    graded_as: str


class SetStatusRequest(BaseModel):
    profile_id: int
    status: str


class RefreshRequest(BaseModel):
    profile_id: int
    card_id: int | None = None  # omit to refresh the whole collection


# ---- auth endpoints ----

@app.get("/api/profiles")
def get_profiles(sess: dict = Depends(current_session)):
    """Admin-only. No longer needs to be public -- login now takes a typed
    username rather than picking from a pre-fetched list."""
    require_admin(sess)
    with db.get_db() as conn:
        return {"profiles": db.list_profiles_public(conn)}


@app.post("/api/login")
def login(req: LoginRequest, response: Response, request: Request):
    _check_login_rate_limit(request.client.host if request.client else "unknown")
    with db.get_db() as conn:
        profile = db.get_profile_by_username(conn, req.username)
        if not profile:
            raise HTTPException(401, "Invalid username or password")

        if profile["password_hash"] is None:
            # first login for this profile sets its password
            salt, hash_ = auth.hash_password(req.password)
            db.set_password(conn, profile["id"], salt, hash_)
        else:
            if not auth.verify_password(req.password, profile["password_salt"], profile["password_hash"]):
                raise HTTPException(401, "Invalid username or password")

        token = auth.new_session_token()
        expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_LIFETIME_DAYS)).isoformat()
        db.create_session(conn, token, profile["id"], expires)

    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", secure=COOKIE_SECURE,
        max_age=SESSION_LIFETIME_DAYS * 24 * 3600, path="/",
    )
    return {"profile_id": profile["id"], "name": profile["name"], "is_admin": bool(profile["is_admin"])}


@app.post("/api/logout")
def logout(response: Response, session: str | None = Cookie(default=None)):
    if session:
        with db.get_db() as conn:
            db.delete_session(conn, session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(sess: dict = Depends(current_session)):
    return sess


@app.post("/api/profiles/me/change-password")
def change_own_password(req: ChangePasswordRequest, sess: dict = Depends(current_session)):
    with db.get_db() as conn:
        profile = db.get_profile(conn, sess["profile_id"])
        if not auth.verify_password(req.old_password, profile["password_salt"], profile["password_hash"]):
            raise HTTPException(401, "Current password is wrong")
        salt, hash_ = auth.hash_password(req.new_password)
        db.set_password(conn, sess["profile_id"], salt, hash_)
    return {"ok": True}


# ---- admin: manage profiles ----

@app.post("/api/admin/profiles")
def admin_create_profile(req: CreateProfileRequest, sess: dict = Depends(current_session)):
    require_admin(sess)
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Username required")
    with db.get_db() as conn:
        if db.get_profile_by_username(conn, name):
            raise HTTPException(409, "That username is already taken")
        try:
            profile_id = db.create_profile(conn, name, is_admin=req.is_admin)
        except Exception:
            raise HTTPException(409, "That username is already taken")
    return {"profile_id": profile_id}


@app.delete("/api/admin/profiles/{profile_id}")
def admin_delete_profile(profile_id: int, sess: dict = Depends(current_session)):
    require_admin(sess)
    with db.get_db() as conn:
        target = db.get_profile(conn, profile_id)
        if not target:
            raise HTTPException(404, "No such profile")
        if target["is_admin"] and db.admin_count(conn) <= 1:
            raise HTTPException(400, "Can't delete the only remaining admin")
        db.delete_profile(conn, profile_id)
    return {"ok": True}


@app.post("/api/admin/profiles/{profile_id}/reset-password")
def admin_reset_password(profile_id: int, req: ResetPasswordRequest, sess: dict = Depends(current_session)):
    require_admin(sess)
    with db.get_db() as conn:
        if not db.get_profile(conn, profile_id):
            raise HTTPException(404, "No such profile")
        salt, hash_ = auth.hash_password(req.new_password)
        db.set_password(conn, profile_id, salt, hash_)
    return {"ok": True}


@app.post("/api/admin/profiles/{profile_id}/rename")
def admin_rename_profile(profile_id: int, req: RenameProfileRequest, sess: dict = Depends(current_session)):
    require_admin(sess)
    new_username = req.new_username.strip()
    if not new_username:
        raise HTTPException(400, "Username required")
    with db.get_db() as conn:
        target = db.get_profile(conn, profile_id)
        if not target:
            raise HTTPException(404, "No such profile")
        existing = db.get_profile_by_username(conn, new_username)
        if existing and existing["id"] != profile_id:
            raise HTTPException(409, "That username is already taken")
        db.rename_profile(conn, profile_id, new_username)
    return {"ok": True}


@app.post("/api/admin/profiles/{profile_id}/set-admin")
def admin_set_admin(profile_id: int, req: SetAdminRequest, sess: dict = Depends(current_session)):
    require_admin(sess)
    with db.get_db() as conn:
        target = db.get_profile(conn, profile_id)
        if not target:
            raise HTTPException(404, "No such profile")
        if target["is_admin"] and not req.is_admin and db.admin_count(conn) <= 1:
            raise HTTPException(400, "Can't remove the only remaining admin")
        conn.execute("UPDATE profiles SET is_admin = ? WHERE id = ?", (int(req.is_admin), profile_id))
    return {"ok": True}


# ---- folders ----

@app.get("/api/folders")
def get_folders(profile_id: int, sess: dict = Depends(current_session)):
    check_access(profile_id, sess)
    with db.get_db() as conn:
        return {"folders": db.list_folders(conn, profile_id)}


@app.post("/api/folders")
def add_folder(req: CreateFolderRequest, sess: dict = Depends(current_session)):
    check_access(req.profile_id, sess)
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    with db.get_db() as conn:
        folder_id = db.create_folder(conn, req.profile_id, name)
    return {"folder_id": folder_id}


@app.delete("/api/folders/{folder_id}")
def remove_folder(folder_id: int, profile_id: int, sess: dict = Depends(current_session)):
    check_access(profile_id, sess)
    with db.get_db() as conn:
        db.delete_folder(conn, profile_id, folder_id)
    return {"ok": True}


@app.post("/api/cards/move")
def move_cards(req: MoveCardsRequest, sess: dict = Depends(current_session)):
    check_access(req.profile_id, sess)
    if not req.card_ids:
        raise HTTPException(400, "No cards specified")
    with db.get_db() as conn:
        db.move_cards(conn, req.profile_id, req.card_ids, req.folder_id)
    return {"ok": True}


# ---- cards ----

@app.get("/api/cards")
def get_cards(profile_id: int, status: str = "owned", sess: dict = Depends(current_session)):
    check_access(profile_id, sess)
    if status not in ("owned", "wanted"):
        raise HTTPException(400, "status must be 'owned' or 'wanted'")
    with db.get_db() as conn:
        cards = db.list_cards(conn, profile_id, status=status)

    totals = {g: 0.0 for g in ebay_api.GRADE_COLUMNS}
    for card in cards:
        for grade, price in card["prices"].items():
            if price is not None:
                totals[grade] += price * card["quantity"]

    return {"cards": cards, "totals": totals}


def _require_ebay_configured():
    if not ebay_api.CLIENT_ID or not ebay_api.CLIENT_SECRET:
        raise HTTPException(
            503,
            "This server's eBay API keys aren't set up yet -- add "
            "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET (see README) before "
            "searching or pricing cards.",
        )


@app.get("/api/search")
def search_cards(q: str, category_id: str | None = None, sess: dict = Depends(current_session)):
    """Results are already grouped into distinct cards with a full
    grade-price table each -- see ebay_api.search()'s docstring. One
    eBay API call total, regardless of how many groups/listings that
    produces (a previous version fetched prices per visible result via a
    separate /api/search-detail endpoint, now removed -- that cost one
    extra call per result on screen). category_id is the search modal's
    "Card Type" picker (Sports vs TCG) -- ebay_api.search() validates it
    against KNOWN_CATEGORY_IDS and falls back to the deployment's default
    for anything else, so an unexpected value here just means "use the
    default," not an error."""
    if not q or len(q.strip()) < 2:
        raise HTTPException(400, "Query too short")
    _require_ebay_configured()
    return {"results": ebay_api.search(q.strip(), category_id=category_id)}


@app.post("/api/cards")
def add_card(req: AddCardRequest, sess: dict = Depends(current_session)):
    check_access(req.profile_id, sess)
    if req.status not in ("owned", "wanted"):
        raise HTTPException(400, "status must be 'owned' or 'wanted'")
    _require_ebay_configured()
    details = ebay_api.fetch_card_details(req.item_id)
    if not details:
        raise HTTPException(404, "Could not load that listing -- it may have ended.")

    with db.get_db() as conn:
        card_id = db.upsert_card(
            conn,
            req.profile_id,
            details["external_id"],
            details["title"],
            details["set_name"],
            details["category"],
            details["image_url"],
            details["product_url"],
            req.quantity,
            folder_id=req.folder_id,
            status=req.status,
        )
        db.set_prices(conn, card_id, details["prices"], now_iso())

    return {"card_id": card_id}


@app.delete("/api/cards/{card_id}")
def remove_card(card_id: int, profile_id: int, sess: dict = Depends(current_session)):
    check_access(profile_id, sess)
    with db.get_db() as conn:
        db.delete_card(conn, profile_id, card_id)
    return {"ok": True}


@app.patch("/api/cards/{card_id}")
def edit_card(card_id: int, req: UpdateCardRequest, sess: dict = Depends(current_session)):
    check_access(req.profile_id, sess)
    if req.quantity < 0:
        raise HTTPException(400, "Quantity can't be negative")
    if req.graded_as not in ebay_api.GRADE_COLUMNS:
        raise HTTPException(400, f"graded_as must be one of {ebay_api.GRADE_COLUMNS}")
    with db.get_db() as conn:
        db.update_card(conn, req.profile_id, card_id, req.quantity, req.graded_as)
    return {"ok": True}


@app.post("/api/cards/{card_id}/status")
def move_card_status(card_id: int, req: SetStatusRequest, sess: dict = Depends(current_session)):
    check_access(req.profile_id, sess)
    if req.status not in ("owned", "wanted"):
        raise HTTPException(400, "status must be 'owned' or 'wanted'")
    with db.get_db() as conn:
        db.set_card_status(conn, req.profile_id, card_id, req.status)
    return {"ok": True}


@app.post("/api/refresh")
def refresh(req: RefreshRequest, sess: dict = Depends(current_session)):
    check_access(req.profile_id, sess)
    _require_ebay_configured()
    with db.get_db() as conn:
        if req.card_id is not None:
            rows = conn.execute(
                "SELECT id, title FROM cards WHERE id = ? AND profile_id = ?",
                (req.card_id, req.profile_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title FROM cards WHERE profile_id = ?", (req.profile_id,)
            ).fetchall()

    updated, failed = [], []
    for row in rows:
        try:
            prices = ebay_api.fetch_grade_prices(ebay_api.strip_grade_tokens(row["title"]))
        except Exception:
            prices = {}
        if not prices:
            failed.append(row["id"])
            continue
        with db.get_db() as conn:
            db.set_prices(conn, row["id"], prices, now_iso())
        updated.append(row["id"])
        time.sleep(ebay_api.REQUEST_DELAY)

    return {"updated": updated, "failed": failed}


@app.post("/api/cards/import")
async def import_csv(file: UploadFile, profile_id: int = Form(...), sess: dict = Depends(current_session)):
    check_access(profile_id, sess)
    _require_ebay_configured()
    content = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))

    added, failed = [], []
    for row in reader:
        product_name = row.get("product-name")
        console_name = row.get("console-name")
        quantity = int(row.get("quantity") or 1)
        if not product_name or not console_name:
            continue

        details = ebay_api.find_product_for_csv_row(product_name, console_name)
        if not details:
            failed.append(product_name)
            continue

        with db.get_db() as conn:
            card_id = db.upsert_card(
                conn,
                profile_id,
                details["external_id"],
                details["title"],
                details["set_name"],
                details["category"],
                details["image_url"],
                details["product_url"],
                quantity,
            )
            db.set_prices(conn, card_id, details["prices"], now_iso())
        added.append(product_name)

    return {"added": added, "failed": failed}


# ---- background auto-refresh ----
# Runs inside the app itself rather than as an external cron job, since the
# refresh endpoint requires a login session and there's no clean way for an
# external script to hold one. This works directly against the DB and
# ebay_api, skipping HTTP/auth entirely -- refreshes every card across every
# profile, not scoped to whoever's logged in (there's no "current session"
# concept for a background job).
#
# Cost note: each card costs one eBay Browse API call per refresh cycle
# (see ebay_api.py for why it's one call, not one per grade). The default
# tier is 5,000 calls/day, app-wide -- so the default here is twice a day
# (43200s) rather than hourly: a 300-card collection costs 300 * 2 = 600
# calls/day that way, versus 7,200/day at hourly. Tune
# CARDVAULT_AUTO_REFRESH_SECONDS to your collection's size -- eBay's free
# "Application Growth Check" can raise the 5,000/day ceiling a lot for an
# app that's already using it efficiently, if hourly ever matters more
# than the extra step of requesting that.
AUTO_REFRESH_INTERVAL_SECONDS = int(os.environ.get("CARDVAULT_AUTO_REFRESH_SECONDS", "43200"))


def refresh_all_profiles_sync():
    if not ebay_api.CLIENT_ID or not ebay_api.CLIENT_SECRET:
        print("[auto-refresh] skipped -- EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set")
        return
    with db.get_db() as conn:
        rows = conn.execute("SELECT id, title FROM cards").fetchall()
    print(f"[auto-refresh] refreshing {len(rows)} card(s) across all profiles...")
    updated, failed = 0, 0
    for row in rows:
        try:
            prices = ebay_api.fetch_grade_prices(ebay_api.strip_grade_tokens(row["title"]))
        except Exception as e:
            print(f"[auto-refresh] error fetching card {row['id']}: {e}")
            prices = {}
        if not prices:
            failed += 1
            continue
        with db.get_db() as conn:
            db.set_prices(conn, row["id"], prices, now_iso())
        updated += 1
        time.sleep(ebay_api.REQUEST_DELAY)
    print(f"[auto-refresh] done: {updated} updated, {failed} failed")


async def auto_refresh_loop():
    while True:
        await asyncio.sleep(AUTO_REFRESH_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(refresh_all_profiles_sync)
        except Exception as e:
            print(f"[auto-refresh] cycle failed: {e}")


@app.on_event("startup")
async def start_auto_refresh():
    if AUTO_REFRESH_INTERVAL_SECONDS > 0:
        asyncio.create_task(auto_refresh_loop())
        print(f"[auto-refresh] scheduled every {AUTO_REFRESH_INTERVAL_SECONDS}s "
              f"(set CARDVAULT_AUTO_REFRESH_SECONDS=0 to disable)")
    else:
        print("[auto-refresh] disabled (CARDVAULT_AUTO_REFRESH_SECONDS=0)")


@app.get("/healthz")
def healthz():
    """Cheap liveness check -- for Docker HEALTHCHECK, TrueNAS, or a
    reverse proxy in front of a public deployment. Deliberately doesn't
    touch the DB or eBay -- just "is the process up and answering"."""
    return {"status": "ok"}


db.init_db()
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
