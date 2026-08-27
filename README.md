# CardVault

A small self-hosted app for tracking a card collection with per-grade
pricing (Ungraded / Grade 7 / Grade 8 / Grade 9 / Grade 9.5 / PSA 10 / BGS 10),
priced from live eBay listings via eBay's official Browse API. SQLite by
default, no scraping, no browser automation, no external services other
than eBay's own API.

This is a fork of an earlier single-household version of the app, reworked
around two things: a pricing source that's actually redistributable to
other people (not scraped), and a deploy path that doesn't require cloning
the repo onto every box that runs it.

## Run it

```bash
cd backend
pip install -r requirements.txt
EBAY_CLIENT_ID=... EBAY_CLIENT_SECRET=... uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open `http://<this-machine's-ip>:8000` in a browser (or `localhost:8000`
if running it directly on your desktop). See "Getting eBay API keys" below
for `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET` -- the app runs without them, but
search/add/refresh return a clear error until they're set.

The SQLite file (`backend/cardvault.db`) is created automatically on first
run and holds your whole collection. Back it up like you would any other
data on the box.

## Getting eBay API keys

Pricing comes from eBay's **Browse API**, via the free eBay Developer
Program:

1. Register at <https://developer.ebay.com> with a business (or personal)
   email. Approval is typically same-day to ~1 business day -- no
   application form describing your use case, unlike some pricing APIs.
2. Under **Application Keys**, create a **Production** keyset. The **App
   ID** is `EBAY_CLIENT_ID`, the **Cert ID** is `EBAY_CLIENT_SECRET`.
3. *(Optional)* Register for the **eBay Partner Network**
   (<https://partnernetwork.ebay.com>) and set `EBAY_EPN_CAMPAIGN_ID` --
   every card's "View listing" link then carries your affiliate tag, so a
   referred purchase earns a small commission.

The free tier is **5,000 API calls/day**, shared across the whole
deployment (not per-user) -- see "How it gets the data" below for what
that means for refresh frequency, and eBay's free "Application Growth
Check" if you outgrow it.

**If you're standing this app up for other people to use, not just
yourself:** the keys above are yours, tied to your own eBay developer
account -- see "A note on redistributing this" below for why that matters.

## Accounts & access

Every account has a username and a password. Login is type-in-both, not
pick-from-a-list -- the account list isn't shown to anyone who isn't
already logged in as an admin, so there's nothing to browse before
signing in. First login to an account sets its password: enter anything
and that becomes the password from then on, so there's no separate
signup step.

**Admin vs regular accounts.** Admins can switch between and view every
account (the dropdown next to the logo, only visible to admins), create
new accounts, rename any account's username, reset anyone's password,
promote/demote other admins, and delete accounts. Regular accounts can
only ever see and manage their own collection -- this is enforced
server-side, not just hidden in the UI, so even a tampered request with
someone else's account ID gets rejected. There's always at least one
admin; the app won't let you delete or demote the last one.

**What this isn't:** there's no password reset via email, and sessions are
plain cookies. Login is rate-limited per IP (10 attempts/minute by
default, `CARDVAULT_LOGIN_RATE_LIMIT`), and the session cookie's `Secure`
flag is controlled by `CARDVAULT_COOKIE_SECURE` -- set that to `true` once
this is actually served over HTTPS (direct TLS, or a reverse proxy in
front terminating it); leave it `false` for plain LAN `http://` access,
where a `Secure` cookie would just silently break login. This still isn't
a hardened public login system (no 2FA, no email-based recovery), but it's
built to be reasonable for more than just one trusted household now.

## Using it

- **Mobile / Add to Home Screen** -- the table becomes a stacked-card list
  on narrow screens, and the app has a manifest + icons so your phone's
  browser offers "Add to Home Screen." On iOS: Safari's share sheet →
  "Add to Home Screen." On Android: Chrome's menu → "Add to Home Screen"
  (or it may prompt automatically). Either way it opens full-screen, no
  browser chrome, like a normal app icon.

- **Profiles** -- the dropdown next to the logo (admins only -- see
  "Accounts & access" above). Each profile has a completely separate
  collection, folders, totals, and password.
- **Folders** -- the chip row under the tabs (e.g. "Sports Cards", "TCG").
  "+ New Folder" creates one; click a chip to filter the table down to
  just that folder, or "All Cards" to see everything. Select cards with
  the checkboxes (or the header checkbox for everything currently
  visible) and a bar appears to move or delete the selection in bulk.
- **Sortable columns** -- click any grade column header (or Qty) to sort
  by it, highest first; click again to reverse. An arrow shows which
  column and direction is active.
- **Edit a card** (the pencil icon) -- correct the quantity you own, or
  mark it as graded and which grade. Once set, that grade's price is what
  counts as the card's "owned value" everywhere (dashboard totals, most
  valuable list) instead of assuming Ungraded.
- **Card title links out** -- click any card's name to open the eBay
  listing it was added from in a new tab (affiliate-tagged, if
  `EBAY_EPN_CAMPAIGN_ID` is set).
- **Dashboard tab** -- unique card count, total quantity, owned value
  (based on each card's actual graded condition, defaulting to Ungraded
  until you mark it otherwise), value broken down by folder, your 10 most
  valuable cards, and the 5 most recently added.
- **Import CSV** -- upload a CSV with `product-name`, `console-name`, and
  `quantity` columns (the same shape a PriceCharting collection export
  uses, if you have one from before). Each row is searched on eBay and
  added with current per-grade prices, into whichever profile is currently
  selected.
- **+ Add Card** -- search by name/set, see thumbnails and each listing's
  own asking price so you can confirm you've got the right card/variant (a
  "View listing" link opens the real eBay listing too), then add it with a
  quantity. Lands in the folder you're currently viewing.
- **↻ Refresh Prices** -- re-searches eBay for every card in the current
  profile's collection and updates current prices, one search per card
  (see "How it gets the data" for the cost of this).
- Each row shows a small bar "curve" across all seven grades so you can
  eyeball where the value jump actually happens for that card.

## How it gets the data

eBay has no fixed "price guide" table the way some pricing sites do --
there's only whatever's currently listed. So each grade's price is
**approximated from live listings**: one search per card (not one per
grade -- see the call-budget note below), bucketing the returned listings
by whichever grade their title mentions ("PSA 10", "BGS 9.5", etc.,
matched by regex; a title mentioning no grading company at all is treated
as raw/Ungraded), then taking the **median** price within each bucket.
That's genuinely noisier than a curated price guide would be -- a
thinly-listed card, or unusual title wording, can come back with gaps or
odd numbers. It's also real, current market data, and -- the actual reason
for building it this way -- something this app is allowed to show to other
people using it. All of this logic lives in `backend/ebay_api.py`; if
eBay's response shape changes, that's the one file to look at.

**Call budget.** The free eBay tier is 5,000 API calls/day, shared across
the whole deployment. At one call per card per refresh, a 300-card
collection on the default hourly auto-refresh (`CARDVAULT_AUTO_REFRESH_SECONDS=3600`)
would use 300 × 24 = 7,200 calls/day -- over budget. Either lengthen the
interval (e.g. every 6 hours: 300 × 4 = 1,200/day) or request eBay's free
Application Growth Check once you know your real usage. The manual "↻
Refresh Prices" button and the "+ Add Card" flow also spend from the same
daily budget.

## A note on redistributing this

eBay's Browse API is meant for exactly this kind of app -- third-party
tools that search and display current listings, with the eBay Partner
Network built specifically to let those tools earn a commission for it.
That's a meaningfully different position than a scraped or paid-API-gated
pricing source: eBay's terms don't block other people from seeing this
data through an app like this the way some pricing sites' do.

That said, this is a good-faith reading, not a legal guarantee. If you're
running this for a genuinely public audience rather than family/friends,
it's worth a quick note to eBay describing the setup (one deployment,
your own developer keys, per-search live listing data, no bulk
redistribution or resale of raw data) and confirming it's fine at whatever
scale you're aiming for.

## Running it in Docker / TrueNAS

```bash
cp .env.example .env   # fill in EBAY_CLIENT_ID / EBAY_CLIENT_SECRET
docker compose up -d --build
```

No browser, no Xvfb, no CAPTCHA-solving step, no `.browser-profile`
directory to persist -- this image is just Python and a couple of pure-HTTP
dependencies (`backend/requirements.txt`), so it builds fast and stays
small. The only thing that needs to survive a container being recreated is
the database: `docker-compose.yml` mounts `./data` on the host to
`/app/data` and points `CARDVAULT_DB_PATH` at it -- don't remove that
without replacing it with something else that persists.

**For TrueNAS SCALE**, see `TRUENAS.md` and `docker-compose.truenas.yaml`
in this repo. Installing there is a straight YAML paste referencing a
pre-built image on GHCR (`.github/workflows/docker-publish.yml` publishes
it on every push) -- no cloning this repo onto the NAS, no building
anything on the box itself.

## Extending it

Straightforward next additions if you want them later:
- **Price history / trend charts** -- the schema only keeps *current*
  prices right now (`current_prices`, one row per card+grade, overwritten
  on refresh). Adding an append-only `price_history` table and a small
  chart per card is a small change from here.
- **Multiple pricing sources** -- `backend/ebay_api.py` is the one module
  the rest of the app talks to for pricing; a second source (e.g. as a
  fallback when eBay's title-based grade bucketing comes back sparse for a
  card) would plug in there without touching `main.py`'s endpoints.
- **A real database for real multi-tenant scale** -- SQLite (one file, one
  writer) is fine for a household or a modest shared deployment; if this
  grows into many concurrent public users, swapping `backend/database.py`
  for Postgres is the next step, not something built in yet.
