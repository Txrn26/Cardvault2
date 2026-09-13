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
2. *(Optional but recommended for a first run)* Under **Application
   Keys**, create a **Sandbox** keyset first and set `EBAY_ENV=sandbox`
   with it. Sandbox has its own (higher) call limits and entirely fake
   listings/prices -- it's a safe way to confirm the app can actually
   authenticate and talk to eBay's API before pointing it at real
   listings, but it can't tell you whether search results or prices make
   sense, since none of the data is real. Whether the Browse API needs
   anything beyond a plain Production keyset to go live (some of eBay's
   Buy APIs need an additional partner/compatibility step, others don't)
   wasn't fully resolvable from outside an actual developer account when
   this was written -- creating the Production keyset in the next step is
   the fastest way to get a real answer; eBay's own UI will say so if
   there's an extra step for this specific API.
3. Under **Application Keys**, create a **Production** keyset. The **App
   ID** is `EBAY_CLIENT_ID`, the **Cert ID** is `EBAY_CLIENT_SECRET`.
4. **Before the Production keyset activates**, eBay requires every app to
   either subscribe to, or explicitly opt out of, "marketplace account
   deletion/closure" notifications (a link on the keyset page; it'll show
   "currently disabled" until this is done). **Opt out** -- that
   notification exists for apps that store data tied to individual eBay
   users' accounts (via per-user OAuth), which this app doesn't do; it
   only ever uses one app-level token, never logs in as an eBay user, and
   never touches eBay account data.
5. *(Optional)* Register for the **eBay Partner Network**
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
- **Card title links out** -- click any card's name to open **live eBay
  search results** for that card in a new tab (affiliate-tagged, if
  `EBAY_EPN_CAMPAIGN_ID` is set), not the one specific listing it happened
  to be added from. A single listing is a moment in time -- it sells, ends,
  or gets pulled, and that page eventually stops being useful; a search
  for the card stays live and current for as long as you own the card.
  (In the search/add-card modal itself, "View listing" does point at one
  specific listing -- that's the right thing there, since you're
  confirming you've got the exact right card/variant before adding it.)
- **Dashboard tab** -- unique card count, total quantity, owned value
  (based on each card's actual graded condition, defaulting to Ungraded
  until you mark it otherwise), value broken down by folder, your 10 most
  valuable cards, and the 5 most recently added.
- **Import CSV** -- upload a CSV with `product-name`, `console-name`, and
  `quantity` columns (the same shape a PriceCharting collection export
  uses, if you have one from before). Each row is searched on eBay and
  added with current per-grade prices, into whichever profile is currently
  selected.
- **+ Add Card** -- pick a Card Type (Sports Trading Cards or TCG/Non-Sport,
  scopes which eBay category actually gets searched), optionally narrow with
  Year/Set, then search by name; results are current eBay listings grouped
  into distinct cards (not one row per raw listing), each already showing
  its own full grade-price table so you can confirm you've got the right
  card/variant before adding ("View listings" opens that card's live eBay
  search). Add with a quantity; lands in the folder you're currently
  viewing.
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

**Multi-card listings.** A "10-Card Lot PSA 9" selling for far more than
any single card would badly skew that grade's median if it slipped into
the same bucket as actual single-card listings. Two layers guard against
that: the search only queries eBay's "Singles"/"Individual Cards"
categories to begin with (not its separate lot/bundle categories), and a
second title-text check (`_is_multi_card_listing`) drops anything that
still reads as a lot ("lot of", "bundle", "complete set", "you pick",
etc.) despite being categorized as a single. Neither is airtight -- a
seller can always word a lot listing unusually -- but between the two,
the common cases are covered. A price that looks obviously too high or
too low for a card is worth a quick click-through to the actual listings
behind it (same as you'd sanity-check any pricing tool).

**Estimated prices.** A brand-new or thinly-collected card can genuinely
have zero listings for a given grade yet (professional grading takes
weeks to months after a card releases, so a just-dropped rookie may have
no PSA 10 sales at all). Rather than leaving that grade blank, it's
estimated from comparable cards -- others sharing the same year and
manufacturer (and, if recognized from a small curated list, the same
insert/subset) -- by scaling this card's own real Ungraded price by that
comparable set's typical grade-premium ratio. An estimated price is
marked with a small `~` in the collection table (hover for the reason)
and is never allowed to overwrite a grade that already has a real,
listing-derived price -- the moment real listings for that grade do show
up on a later refresh, they replace the estimate outright. Like the grade
and lot-detection matching above, comparable-card matching is a title-text
heuristic (`ebay_api.py`'s `_comparable_signature`/`_get_ratio_profile`),
not a real product catalog -- an unrecognized manufacturer skips
estimation for that card entirely rather than guessing from too little.

**Call budget.** eBay's APIs have no separate paid tier to buy your way
past this -- API access itself is free either way, the only lever is a
call-volume *limit*, not a price. Every new app starts at 5,000 calls/day,
shared across the whole deployment (not per-user). At one call per card
per refresh, the default auto-refresh interval
(`CARDVAULT_AUTO_REFRESH_SECONDS=43200`, twice a day) keeps a 300-card
collection at 300 × 2 = 600 calls/day, with plenty of headroom left over
for manual "↻ Refresh Prices" clicks and "+ Add Card" searches, which
spend from the same daily budget. Set it back to `3600` (hourly) if you
want fresher prices and have a small enough collection to still fit --
300 cards hourly is 300 × 24 = 7,200/day, over budget; ~200 cards hourly
(4,800/day) is right at the edge.

Estimated prices (above) add a little to this, but far less than
one-call-per-card -- the comparable-card search behind an estimate is
cached ~6h per distinct (year, manufacturer, insert) combo, so a whole
refresh sweep only pays for each combo actually needing an estimate once,
not once per card. A realistic collection might add a low single-digit
percent to the numbers above.

If a collection genuinely outgrows 5,000/day, eBay's free **Application
Growth Check** (Developer Program → your app → request a limit increase,
describing real usage) can raise the ceiling substantially for an app
that's already calling the API efficiently -- there's no paid plan to
skip that step with, it's the only path past the default limit.

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

**Efficient use.** eBay's API License Agreement specifically calls out
caching locally and not re-fetching data that was just fetched --
`backend/ebay_api.py` caches every call for 5 minutes (a query repeated
within that window, e.g. two visible search results sharing a title, or a
double-clicked refresh, is served from that cache instead of hitting eBay
again), and grade prices themselves are cached indefinitely between
scheduled refreshes in the `current_prices` table rather than fetched on
every page view. Neither is a magic compliance guarantee either, but it's
the actual behavior eBay's terms ask for, not just a rate-limit
workaround.

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

## Where else this can run

The app itself is a plain, stateless container: one process, config via
env vars, one directory (`/app/data`) it needs persisted. Nothing in it is
TrueNAS-specific -- "Custom App" is just TrueNAS's name for "run this
docker-compose file," so the same image and the same `docker-compose.yml`
work unchanged on:

- **A real web server / VPS** -- same `docker compose up -d`, with a
  reverse proxy (Caddy, Nginx, Traefik) in front for TLS and a domain name,
  and `CARDVAULT_COOKIE_SECURE=true` once that's in place.
- **TrueNAS's official Apps catalog** (the ones with icons in the Apps
  store, as opposed to Custom App) -- that's a separate packaging format
  (a chart with `app.yaml`/`questions.yaml` for the nicer guided-install
  UI) wrapping the *same* image, not a different build of the app. Worth
  doing later if this becomes something you want a friendlier install
  experience for; not needed for it to work today.
- Any other Docker-based host -- Portainer, Coolify, a Kubernetes cluster,
  Fly.io, Railway, a plain systemd + Docker box, etc. -- same image, same
  env vars.

The one real fork in the road as usage grows isn't the deployment
mechanism, it's the database: SQLite (a single file, a single writer) is
what makes "just a container + a data volume" this simple, and it's fine
up through a household or a modest shared deployment. Genuinely many
concurrent public users writing to their own collections at once is where
that stops being true and Postgres becomes the right call -- see
"Extending it" below. That's a `backend/database.py` change, not a
redeploy-everything change; the rest of the app (including this same
Docker packaging) doesn't need to be rebuilt around it.

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
