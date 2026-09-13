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

Search results are grouped, not raw: eBay has no catalog/product id the
way PriceCharting did, so search() fetches one page of listings and
collapses them into distinct "cards" by title (grade wording stripped
out), each shown with its own grade-price table computed from exactly the
listings already in hand -- one API call for the whole search, not one
per result. Old versions of this module called fetch_grade_prices()
separately for every visible search result (a "search-detail" endpoint,
now removed); that gave a live-updating price per listing but cost as
many calls as there were results on screen, and still showed one row per
raw listing rather than one row per card the way the old PriceCharting
catalog view did.
"""

import base64
import os
import re
import statistics
import time
import urllib.parse
from collections import Counter
from threading import Lock

import requests

GRADE_COLUMNS = ["Ungraded", "Grade 7", "Grade 8", "Grade 9", "Grade 9.5", "PSA 10", "BGS 10"]

# Originally this covered both sports cards (261328, "Sports Trading
# Cards") and TCGs (183454, "CCG Individual Cards") in one search, mirroring
# the old app's combined PriceCharting/SportsCardsPro coverage -- but
# item_summary/search rejects more than one category_id per request
# (errorId 12030, discovered against Production: "allowedMaxCategories":
# "1"), which the Sandbox environment doesn't enforce/expose. So this is
# one category, overridable via env for TCG collections -- sports cards by
# default since that's this deployment's actual use.
CATEGORY_ID = os.environ.get("EBAY_CATEGORY_ID", "").strip() or "261328"
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
# Two layers, because neither alone is reliable: CATEGORY_ID above already
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


def _grade_price_table(items: list[dict]) -> dict:
    """Median price per grade bucket, from a list of already-fetched,
    already-filtered eBay item dicts (token match + lot filter both
    applied by the caller). Pulled out on its own since both search()'s
    per-group tables and fetch_grade_prices()'s single table need exactly
    this same bucket-then-median step."""
    buckets: dict[str, list[float]] = {g: [] for g in GRADE_COLUMNS}
    for item in items:
        grade = _classify_grade(item.get("title", ""))
        price = _extract_price(item)
        if grade and price is not None:
            buckets[grade].append(price)
    return {grade: round(statistics.median(prices), 2) for grade, prices in buckets.items() if prices}


# Different sellers word the exact same card differently enough (word
# order, "(RC)", the team name tacked on, a stray dash) that requiring an
# exact match after stripping grades split one real card across several
# groups -- e.g. "2026 Topps Chrome #FS-3 JJ Wetherholt Future Stars" and
# "2026 Topps Chrome - Future Stars JJ Wetherholt #FS-3 (RC)" are the same
# card. Grouping instead needs *tolerant* matching (unordered token
# overlap) -- but tolerant matching alone would also merge a numbered
# "/75" parallel or a named finish like "Purple Wave" into the base
# card's group, silently pulling its price into (or out of) the ordinary
# card's median. Two hard differentiators fix that: any purely numeric
# token that appears in only one of the two titles (almost always a
# print run number), and a small curated list of parallel/finish names
# that are specific enough sellers only use them for that actual variant.
#
# "Refractor" deliberately isn't in that list, despite being a real named
# Topps Chrome parallel tier of its own -- in practice it's also common
# generic flavor text sellers tack onto any shiny Chrome card (including
# onto other named parallels, e.g. "Logofractor ... Refractor RC")
# regardless of whether the card is actually that specific tier, so
# forcing a split on it caused more false fragmentation (two wordings of
# the same Logofractor card, kept apart) than it prevented. It's just an
# ordinary token now, folded into the overlap ratio like anything else.
#
# Neither list is exhaustive -- like the grade and lot-detection patterns
# above, this is a heuristic over title text, not a real product catalog,
# so an unlisted parallel name can still merge into a base card's group;
# a price that looks off for what should be an ordinary listing is worth
# a quick click into "View listings" to check.
_PARALLEL_KEYWORDS = {
    "xfractor", "superfractor", "negative", "negatives",
    "prizm", "prism", "atomic", "wave", "shimmer", "mojo", "holo",
    "holofoil", "rainbow", "foilboard", "kaboom", "downtown", "sparkle",
    "cracked", "ice", "mummy", "logofractor", "velocity", "independence",
    "canary", "fuchsia", "clearly", "tiedye",
    "gold", "black", "white", "silver", "bronze", "pink", "purple",
    "orange", "green", "blue", "red", "yellow", "teal", "magenta", "aqua", "camo",
}
# "rc"/"rookie" are status labels some sellers add and others don't for
# the exact same card -- noise for grouping purposes, unlike the words above.
_GROUP_STOPWORDS = _STOPWORDS | {"rc", "rookie"}
_GROUP_OVERLAP_THRESHOLD = 0.75


def _significant_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower())
            if len(t) > 1 and t not in _GROUP_STOPWORDS}


def _same_card(tokens_a: set[str], tokens_b: set[str]) -> bool:
    """Whether two listings' significant title tokens are close enough to
    treat as the same card for grouping -- see the block comment above."""
    diff = tokens_a ^ tokens_b
    if diff & _PARALLEL_KEYWORDS:
        return False
    if any(t.isdigit() for t in diff):
        return False
    smaller = min(len(tokens_a), len(tokens_b))
    return smaller > 0 and len(tokens_a & tokens_b) / smaller >= _GROUP_OVERLAP_THRESHOLD


def _cluster_by_card(items: list[dict]) -> list[dict]:
    """Groups listings into distinct cards: each item joins the first
    existing cluster its title is close enough to (_same_card), or starts
    a new one. One pass, compared against cluster anchors (the first
    item's tokens, not updated as more items join, so a cluster can't
    slowly drift into matching an unrelated title) -- fine at the scale of
    one search's worth of listings (dozens, not thousands)."""
    clusters: list[dict] = []
    for item in items:
        base_title = strip_grade_tokens(item.get("title", ""))
        tokens = _significant_tokens(base_title)
        if not tokens:
            continue
        cluster = next((c for c in clusters if _same_card(tokens, c["tokens"])), None)
        if cluster is None:
            cluster = {"tokens": tokens, "items": []}
            clusters.append(cluster)
        cluster["items"].append(item)
    return clusters


# The two eBay category ids this app knows about -- offered as a "Card
# Type" choice in the search modal so a search can be scoped to whichever
# actually matches what's being searched for, instead of being locked to
# whatever EBAY_CATEGORY_ID happens to be set deployment-wide. Kept to
# exactly these two (validated in _fetch_filtered_items below) rather than
# accepting any client-supplied id -- there's no reason a logged-in user's
# browser should be able to point this deployment's eBay calls at an
# arbitrary category.
KNOWN_CATEGORY_IDS = {
    "261328": "Sports Trading Cards",
    "183454": "TCG / Non-Sport Cards",
}


def _fetch_filtered_items(query: str, limit: int, category_id: str | None = None) -> list[dict]:
    """One search call, with the token-match and lot filters already
    applied -- the shared first step behind both search() and
    fetch_grade_prices(). category_id overrides the deployment's default
    (CATEGORY_ID) for this one call -- only ever from the search modal's
    "Card Type" picker, and only if it's one of KNOWN_CATEGORY_IDS."""
    resolved_category = category_id if category_id in KNOWN_CATEGORY_IDS else CATEGORY_ID
    tokens = _query_tokens(query)
    data = _get("/item_summary/search", {
        "q": query, "category_ids": resolved_category, "limit": min(limit, 200),
    })
    items = []
    for item in data.get("itemSummaries", []):
        title = item.get("title", "")
        if not title or not _row_matches_query(title, tokens):
            continue
        if _is_multi_card_listing(title):
            continue  # a lot/bundle isn't "one card" -- exclude entirely
        items.append(item)
    return items


# ---- public API ----

def search(query: str, limit: int = 30, sample_size: int = 200, category_id: str | None = None) -> list[dict]:
    """One row per distinct card, not one per raw eBay listing -- eBay has
    no catalog/product page the way PriceCharting did, so "distinct card"
    means grouping same-search listings by title with grade wording
    stripped out, then giving each group its own grade-price table
    (median per grade, from that group's own listings). One search call
    total regardless of how many groups or listings that produces --
    critical, since an earlier version of this ran an extra API call per
    *visible result* to fill in prices, which could mean 60+ calls for a
    single search."""
    items = _fetch_filtered_items(query, sample_size, category_id)
    clusters = _cluster_by_card(items)

    results = []
    for cluster in clusters:
        group_items = cluster["items"]
        # representative listing: whichever exact title wording is most
        # common in this group (closest to "how most sellers word this
        # card"), first-seen as the tiebreak -- just for the image/display
        # title and search link, the price table itself uses every listing
        # in the group.
        titles = [it.get("title", "") for it in group_items]
        rep_title = Counter(titles).most_common(1)[0][0]
        rep_item = next(it for it in group_items if it.get("title") == rep_title)
        image_url = _extract_image(rep_item)
        results.append({
            "title": rep_title,
            "set_name": None,  # eBay listings don't expose a clean "set" field
            # a live search for this card, not one specific (eventually-gone)
            # listing -- consistent with the permanent link stored on add,
            # see card_search_url()'s comment below.
            "url": card_search_url(strip_grade_tokens(rep_title) or rep_title),
            "item_id": rep_item.get("itemId"),
            "image_url": image_url,
            "image_url_large": image_url,
            "category": _extract_category(rep_item),
            "prices": _grade_price_table(group_items),
            "listing_count": len(group_items),
            "source": "eBay",
        })
    # most-listed first -- a card with 15 current listings is a much safer
    # "yes, this is really it" match than one with a single oddly-worded ad
    results.sort(key=lambda r: r["listing_count"], reverse=True)
    return results[:limit]


def fetch_grade_prices(query: str, sample_size: int = 200) -> dict:
    """The per-grade price table for one specific card: one search,
    bucketed by grade mentioned in each listing's title, median price per
    bucket. Used by refresh (one card at a time, on a schedule) and by
    add_card (via fetch_card_details, confirming the table against the
    exact listing being added)."""
    return _grade_price_table(_fetch_filtered_items(query, sample_size))


# ---- comparable-card price estimation ----
#
# A brand-new or thinly-collected card can have zero listings for a given
# grade (confirmed against Production: a just-released rookie with no
# PSA 10 sales yet), leaving that grade blank. Instead of leaving it
# blank, estimate it from *comparable* cards -- same year/manufacturer,
# ideally same insert, across different players -- scaling this card's
# own real Ungraded price by that comparable set's typical grade-premium
# ratio. Deliberately does NOT try to strip a player's name out of a
# title to find "the insert name" generically -- a 2-word Titlecase
# insert name ("Future Stars") and a 2-word Titlecase player name are
# structurally identical in plain text, so there's no reliable way to
# tell them apart without real name data this app doesn't have. Same
# spirit as _PARALLEL_KEYWORDS/_LOT_PATTERN above: a curated list that
# only ever recognizes what it already knows, and degrades to a coarser
# (still real, never wrong-in-a-way-that-mixes-unrelated-products) scope
# for anything it doesn't -- an unlisted manufacturer skips estimation
# entirely (fails safe), an unlisted insert just falls back to the
# broader year+manufacturer scope instead of erroring.
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
_MANUFACTURERS = [
    "topps", "bowman", "panini", "upper deck", "donruss", "leaf",
    "fanatics", "score", "stadium club",
]
_KNOWN_INSERTS = [
    "future stars", "rated rookie", "rated rookies", "diamond kings",
    "elite extra edition", "downtown", "kaboom", "national treasures",
    "clearly authentic", "1st bowman", "chrome update", "black gold",
    "heritage", "pristine", "sapphire",
]


def _comparable_signature(title: str) -> tuple[str, str, str | None] | None:
    """(year, manufacturer, insert_or_None) -- the scope estimation
    searches for comparable cards within. None means a year or
    manufacturer couldn't be confidently found, so estimation is skipped
    for this card entirely rather than guessing from too little."""
    lowered = title.lower()
    year_match = _YEAR_RE.search(title)
    if not year_match:
        return None
    manufacturer = next((m for m in _MANUFACTURERS if m in lowered), None)
    if not manufacturer:
        return None
    insert = next((i for i in _KNOWN_INSERTS if i in lowered), None)
    return (year_match.group(1), manufacturer, insert)


# A relative multiplier (this grade costs ~3x Ungraded) changes far more
# slowly than an absolute price does, so this cache's TTL is much longer
# than _cache's -- and it's what actually keeps a full collection refresh
# from re-running the comparable search once per card that happens to
# share a signature, instead of once per distinct signature.
_RATIO_CACHE_TTL_SECONDS = 21600  # 6h
_MIN_COMPARABLE_CARDS = 3
_ratio_cache_lock = Lock()
_ratio_cache: dict[tuple, tuple[float, dict]] = {}


def _get_ratio_profile(year: str, manufacturer: str, insert: str | None) -> dict:
    """{grade: ratio-to-Ungraded}, computed from other cards sharing this
    (year, manufacturer[, insert]) signature. Every ratio is anchored to
    that *same comparable card's own* real Ungraded price specifically
    (not "whatever grade happened to be its lowest") -- a comparable
    lacking a real Ungraded price of its own is skipped rather than
    anchored to some other grade, since ratios computed against different
    baselines aren't actually comparable to each other and averaging them
    would be quietly wrong, not just noisy. Requires >= _MIN_COMPARABLE_CARDS
    comparables to agree on a grade before trusting it; grades that don't
    clear that bar are simply left out of the profile."""
    key = (year, manufacturer, insert)
    now = time.time()
    with _ratio_cache_lock:
        cached = _ratio_cache.get(key)
        if cached and now - cached[0] < _RATIO_CACHE_TTL_SECONDS:
            return cached[1]

    query = " ".join(filter(None, [year, manufacturer, insert]))
    items = _fetch_filtered_items(query, 200)
    clusters = _cluster_by_card(items)

    ratios: dict[str, list[float]] = {g: [] for g in GRADE_COLUMNS if g != "Ungraded"}
    for cluster in clusters:
        if len(cluster["items"]) < 2:
            continue  # a single listing is too noisy to trust as its own "comparable card"
        table = _grade_price_table(cluster["items"])
        ungraded = table.get("Ungraded")
        if not ungraded or ungraded <= 0:
            continue
        for grade, price in table.items():
            if grade != "Ungraded":
                ratios[grade].append(price / ungraded)

    profile = {
        grade: round(statistics.median(values), 3)
        for grade, values in ratios.items() if len(values) >= _MIN_COMPARABLE_CARDS
    }
    with _ratio_cache_lock:
        _ratio_cache[key] = (now, profile)
    return profile


def fill_missing_grades(
    title: str, real_prices: dict, protect_grades: frozenset = frozenset()
) -> tuple[dict, set]:
    """Fills grades missing from real_prices (and not in protect_grades --
    see the comment at each call site in main.py for why that exists) with
    an estimate: this card's own real Ungraded price, scaled by a
    comparable-cards' grade-premium ratio. Returns (merged_prices,
    estimated_grade_names); merged_prices is exactly real_prices,
    untouched, whenever there's no real Ungraded price on this card to
    anchor an estimate to, or no comparable signature/ratio can be found --
    fails safe, a grade with nothing real to estimate from is simply left
    blank, same as before this feature existed, never a guessed number."""
    baseline = real_prices.get("Ungraded")
    missing = [
        g for g in GRADE_COLUMNS
        if g != "Ungraded" and g not in real_prices and g not in protect_grades
    ]
    if not baseline or baseline <= 0 or not missing:
        return real_prices, set()

    signature = _comparable_signature(title)
    if not signature:
        return real_prices, set()
    ratios = _get_ratio_profile(*signature)

    merged = dict(real_prices)
    estimated = set()
    for grade in missing:
        if grade in ratios:
            merged[grade] = round(baseline * ratios[grade], 2)
            estimated.add(grade)
    return merged, estimated


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
