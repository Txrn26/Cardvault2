"""
eBay Browse API client -- replaces the old PriceCharting/SportsCardsPro
scraper entirely. No HTML parsing, no browser automation, no persistent
"solve the CAPTCHA once" Chrome profile: everything here is a plain JSON
request against eBay's own documented, ToS-covered API.

Auth is the OAuth2 "client credentials" grant -- one app-level token for
the whole deployment (not per-user), obtained from EBAY_CLIENT_ID /
EBAY_CLIENT_SECRET and cached until it's close to expiring. See README for
where to get those (eBay Developer Program: free, self-serve, ~1 business
day approval -- nothing like PriceCharting's paid-subscription-gated token
or TCGplayer's closed application process).

Because eBay has no fixed "price guide" table the way PriceCharting did,
every card's per-grade prices are *approximated* from live listings: one
search per card, bucketed by grade mentioned in each listing's title,
median price per bucket. Noisier than a curated guide, but real market
data, redistributable to other people using the app, and free.

One search call per card (not one per grade) is a deliberate budget
choice: eBay's free tier is 5,000 calls/day, app-wide. See README for the
math on tuning CARDVAULT_AUTO_REFRESH_SECONDS to a collection's size.
"""

import base64
import os
import re
import statistics
import time
import urllib.parse
from threading import Lock

import requests

GRADE_COLUMNS = ["Ungraded", "Grade 7", "Grade 8", "Grade 9", "Grade 9.5", "PSA 10", "BGS 10"]

# Trading Card Singles, CCG Individual Cards -- covers both sports cards and
# TCGs in one search, same as the old app's combined PriceCharting/
# SportsCardsPro coverage.
CATEGORY_IDS = "261328,183454"
MARKETPLACE = "EBAY_US"

# Polite pacing between calls issued back-to-back in a loop (search-detail
# streaming through visible results, refresh sweeping a collection) -- not
# needed to dodge bot detection like the old REQUEST_DELAY was, just to
# keep from bursting the daily call budget in one breath.
REQUEST_DELAY = 0.3

CLIENT_ID = os.environ.get("EBAY_CLIENT_ID")
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET")
EPN_CAMPAIGN_ID = os.environ.get("EBAY_EPN_CAMPAIGN_ID")

# "sandbox" points this whole module at eBay's Sandbox instead of
# Production -- a separate keyset (generated on the same Application Keys
# page, Sandbox tab), separate call limits (higher than Production's, so
# it's fine to hammer while testing), and entirely fake listings/prices.
# Good for confirming the OAuth/request/response plumbing works at all
# before pointing at real data; useless for judging whether the grade
# bucketing or lot filtering actually behaves sensibly, since there's no
# real card data in Sandbox to test that against -- that part only gets a
# real answer against Production, ideally starting with a couple of small,
# manually-checked searches rather than trusting it blind on day one.
SANDBOX = os.environ.get("EBAY_ENV", "production").strip().lower() == "sandbox"

TOKEN_URL = (
    "https://api.sandbox.ebay.com/identity/v1/oauth2/token" if SANDBOX
    else "https://api.ebay.com/identity/v1/oauth2/token"
)
API_BASE = (
    "https://api.sandbox.ebay.com/buy/browse/v1" if SANDBOX
    else "https://api.ebay.com/buy/browse/v1"
)

_token_lock = Lock()
_token: str | None = None
_token_expires_at = 0.0


class NotConfigured(Exception):
    """EBAY_CLIENT_ID / EBAY_CLIENT_SECRET aren't set on this deployment."""


def _get_token() -> str:
    global _token, _token_expires_at
    if not CLIENT_ID or not CLIENT_SECRET:
        raise NotConfigured("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set")
    with _token_lock:
        if _token and time.time() < _token_expires_at:
            return _token
        basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        resp = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _token = data["access_token"]
        # renew a couple minutes early rather than cutting it exactly at expiry
        _token_expires_at = time.time() + int(data.get("expires_in", 7200)) - 120
        return _token


def _headers() -> dict:
    headers = {
        "Authorization": f"Bearer {_get_token()}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
    }
    if EPN_CAMPAIGN_ID:
        # asks eBay to return itemAffiliateWebUrl on every result, already
        # wrapped with the campaign's tracking -- no manual URL-building.
        headers["X-EBAY-C-ENDUSERCTX"] = f"affiliateCampaignId={EPN_CAMPAIGN_ID}"
    return headers


# eBay's API License Agreement asks apps to cache locally and avoid
# re-fetching data that was already just fetched -- this is what actually
# does that, for every call this module makes (search, grade-price sweep,
# single-item lookup). A short TTL, not a long-lived store: it's here to
# collapse genuine near-duplicate calls (search-detail streaming through
# visible results, a double-clicked refresh, two cards that happen to
# share a title), not to serve stale prices. Capped in size so a
# long-running process with heavy, varied search traffic can't grow this
# unbounded.
_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 500
_cache_lock = Lock()
_cache: dict[tuple, tuple[float, dict]] = {}


def _get(path: str, params: dict) -> dict:
    key = (path, tuple(sorted(params.items())))
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

    resp = requests.get(f"{API_BASE}{path}", headers=_headers(), params=params, timeout=15)
    if not resp.ok:
        # eBay's error body (a JSON array of {errorId, message, longMessage,
        # parameters}) is far more useful than the bare status code --
        # surface it in the exception instead of losing it to
        # raise_for_status(), since that's what actually says *why* eBay
        # rejected the request (bad category id, malformed filter, etc.).
        raise RuntimeError(
            f"eBay API {resp.status_code} for {path} params={params}: {resp.text[:2000]}"
        )
    data = resp.json()

    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            oldest_key = min(_cache, key=lambda k: _cache[k][0])
            del _cache[oldest_key]
        _cache[key] = (now, data)
    return data


# ---- grade classification from listing titles ----

# Checked highest-value grade first -- a title that happens to mention more
# than one number (card number, print run, etc.) shouldn't downgrade an
# actual PSA 10 match.
_GRADE_PATTERNS = [
    ("PSA 10", re.compile(r"\bpsa\s*10\b", re.I)),
    ("BGS 10", re.compile(r"\b(bgs|beckett)\s*10\b", re.I)),
    ("Grade 9.5", re.compile(r"\b(psa|bgs|sgc|beckett)\s*9\.5\b", re.I)),
    ("Grade 9", re.compile(r"\b(psa|bgs|sgc|beckett)\s*9\b", re.I)),
    ("Grade 8", re.compile(r"\b(psa|bgs|sgc|beckett)\s*8\b", re.I)),
    ("Grade 7", re.compile(r"\b(psa|bgs|sgc|beckett)\s*7\b", re.I)),
]
_ANY_GRADE_MENTION = re.compile(r"\b(psa|bgs|sgc|beckett|cgc)\s*\d", re.I)
_GRADE_STRIP = re.compile(r"\b(psa|bgs|sgc|beckett|cgc)\s*\d+(\.\d+)?\b", re.I)


def _classify_grade(title: str) -> str | None:
    """Best-guess grade bucket for a listing from its title text."""
    for grade, pattern in _GRADE_PATTERNS:
        if pattern.search(title):
            return grade
    if _ANY_GRADE_MENTION.search(title):
        return None  # graded by something/some grade we don't bucket -- don't guess
    return "Ungraded"


def strip_grade_tokens(title: str) -> str:
    """Base search text for re-searching a card later (search-detail,
    refresh). The stored title is usually the exact listing title picked
    when the card was added, which may itself name one specific grade --
    stripping that out keeps the re-search from being accidentally scoped
    to just that one grade instead of the card in general."""
    return re.sub(r"\s+", " ", _GRADE_STRIP.sub("", title)).strip()


# ---- loose query matching (eBay's own search is as loose as PriceCharting's was) ----

_STOPWORDS = {"and", "the", "of", "a", "an", "in", "on", "for", "with", "&"}


def _query_tokens(query: str) -> list[str]:
    return [t for t in re.split(r"\s+", query.strip().lower()) if len(t) > 1 and t not in _STOPWORDS]


def _row_matches_query(text: str, tokens: list[str]) -> bool:
    """Require every distinct word the user typed to actually appear in the
    listing title -- same idea as the old scraper's row filter, needed for
    the same reason: eBay's search matches loosely across everything that
    shares a word with the query, not just close matches."""
    lowered = text.lower()
    return all(t in lowered for t in tokens)


# ---- field extraction ----

def _extract_image(item: dict) -> str | None:
    return (item.get("image") or {}).get("imageUrl")


def _extract_price(item: dict) -> float | None:
    value = (item.get("price") or {}).get("value")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_category(item: dict) -> str | None:
    cats = item.get("categories") or []
    return cats[0].get("categoryName") if cats else None


# ---- the link stored/shown for a card ----
#
# A single listing's URL isn't a stable thing to store long-term: eBay
# listings end (sold, expired, pulled) and that page eventually stops
# being useful, sometimes fairly soon. A card's title/finish/etc. as a
# *search* stays live indefinitely and shows every current listing for it
# instead of one (possibly already-gone) seller's -- so that's what gets
# stored as the card's permanent link, built fresh from its title rather
# than reusing whichever specific listing was picked at add-time. The
# specific-listing links in search results (search()'s "View listing")
# are a different, transient case -- fine to point at one exact listing
# there, since that's about confirming you've got the right card/variant
# in the moment of adding it, not something stored for later.
#
# eBay US's rotation ID (mkrid) below is a fixed constant published by
# eBay for exactly this kind of manually-built tracking link -- if
# MARKETPLACE is ever changed to a non-US site, this would need updating
# to that marketplace's own rotation ID.
_EPN_ROTATION_ID = "711-53200-19255-0"


def card_search_url(query: str) -> str:
    # Deliberately always the real site, even in Sandbox mode -- Sandbox
    # is a dev-only testing toggle (see SANDBOX above), not something a
    # real end user would ever be looking at this link under.
    plain = "https://www.ebay.com/sch/i.html?_nkw=" + urllib.parse.quote_plus(query)
    if not EPN_CAMPAIGN_ID:
        return plain
    return (
        f"https://rover.ebay.com/rover/1/{_EPN_ROTATION_ID}/1"
        f"?campid={urllib.parse.quote_plus(EPN_CAMPAIGN_ID)}&toolid=10001"
        f"&mpre={urllib.parse.quote_plus(plain)}"
    )


# ---- multi-card listing (lot/bundle) filtering ----
#
# Two layers, because neither alone is reliable: CATEGORY_IDS above already
# points at eBay's "Singles" categories rather than its separate lot/bundle
# categories, but sellers regularly list a multi-card lot under Singles
# anyway (mistake, or just for the extra search visibility) -- one of those
# slipping into a grade's price bucket means that grade's "price" is really
# a 10-card lot's price. This title-text check is the second layer, for
# whatever the category filter alone doesn't catch. Not perfect either --
# a title can always word this unusually -- but it catches the common
# phrasing. Deliberately conservative (whole-word phrases, not bare
# numbers) to avoid false-positives on a legitimate single card's title
# (a card number, a print run, a year all contain digits too).
_LOT_PATTERN = re.compile(
    r"\b(lot of|lots of|card lot|\d+[\s-]?card\s+lot|bundle|you\s+pick|"
    r"complete\s+set|full\s+set|set\s+of\s+\d|mixed\s+lot|assorted|wholesale)\b",
    re.I,
)


def _is_multi_card_listing(title: str) -> bool:
    return bool(_LOT_PATTERN.search(title))


# ---- public API ----

def search(query: str, limit: int = 60) -> list[dict]:
    """Search current eBay listings for trading cards matching query. Each
    result is one specific listing (its own price/condition/seller) --
    there's no PriceCharting-style single catalog page per card on eBay."""
    tokens = _query_tokens(query)
    data = _get("/item_summary/search", {
        "q": query, "category_ids": CATEGORY_IDS, "limit": min(limit, 200),
    })
    results = []
    for item in data.get("itemSummaries", []):
        title = item.get("title", "")
        if not title or not _row_matches_query(title, tokens):
            continue
        if _is_multi_card_listing(title):
            continue  # a lot/bundle isn't "one card" -- don't offer it to add as one
        image_url = _extract_image(item)
        results.append({
            "title": title,
            "set_name": None,  # eBay listings don't expose a clean "set" field
            "url": item.get("itemAffiliateWebUrl") or item.get("itemWebUrl"),
            "item_id": item.get("itemId"),
            "image_url": image_url,
            "image_url_large": image_url,
            "category": _extract_category(item),
            "price": _extract_price(item),
            "source": "eBay",
        })
        if len(results) >= limit:
            break
    return results


def fetch_grade_prices(query: str, sample_size: int = 100) -> dict:
    """The per-grade price table for a card: one search, bucketed by grade
    mentioned in each listing's title, median price per bucket. Used by
    both search-detail (one card at a time, from the search modal) and
    refresh (one card at a time, on a schedule)."""
    tokens = _query_tokens(query)
    data = _get("/item_summary/search", {
        "q": query, "category_ids": CATEGORY_IDS, "limit": sample_size,
    })
    buckets: dict[str, list[float]] = {g: [] for g in GRADE_COLUMNS}
    for item in data.get("itemSummaries", []):
        title = item.get("title", "")
        if not title or not _row_matches_query(title, tokens):
            continue
        if _is_multi_card_listing(title):
            continue  # a lot's price would badly skew that grade's median
        grade = _classify_grade(title)
        price = _extract_price(item)
        if grade and price is not None:
            buckets[grade].append(price)
    return {grade: round(statistics.median(prices), 2) for grade, prices in buckets.items() if prices}


def fetch_item(item_id: str) -> dict | None:
    """Authoritative single-item lookup, used when adding a card -- never
    trust client-supplied title/image (same principle the old scraper
    followed for product pages), confirm against eBay directly."""
    try:
        item = _get(f"/item/{urllib.parse.quote(item_id, safe='')}", {})
    except requests.RequestException:
        return None
    title = item.get("title")
    if not title:
        return None
    return {
        "item_id": item_id,
        "title": title,
        "set_name": None,
        "category": _extract_category(item),
        "image_url": _extract_image(item),
        "product_url": item.get("itemAffiliateWebUrl") or item.get("itemWebUrl"),
    }


def fetch_card_details(item_id: str) -> dict | None:
    """Everything needed to add a card: confirm the listing exists, then
    sweep for a full per-grade price table using its (grade-stripped)
    title as the search text."""
    item = fetch_item(item_id)
    if not item:
        return None
    base_title = strip_grade_tokens(item["title"]) or item["title"]
    prices = fetch_grade_prices(base_title)
    return {
        "external_id": item_id,
        "title": base_title,
        "set_name": item["set_name"],
        "category": item["category"],
        "image_url": item["image_url"],
        "product_url": card_search_url(base_title),
        "prices": prices,
    }


def find_product_for_csv_row(product_name: str, console_name: str) -> dict | None:
    """Used by CSV import (kept compatible with the same product-name/
    console-name column headers the old PriceCharting-export importer
    used): search eBay for the best match, then fetch its full details."""
    query = f"{product_name} {console_name}".strip()
    results = search(query, limit=1)
    if not results:
        return None
    time.sleep(REQUEST_DELAY)
    return fetch_card_details(results[0]["item_id"])
