# Deploying CardVault on TrueNAS SCALE

Covers 24.10 (Electric Eel) and later -- the Docker-Compose-based Apps
system, not the old Kubernetes one.

Unlike the original CardVault, there's no git clone or `docker build` step
on the NAS at all. GitHub Actions builds the image on every push and
publishes it to GitHub Container Registry (GHCR); TrueNAS just pulls it by
name. Installing is: create a dataset, get eBay API keys, paste one YAML
file.

## 1. Get eBay API keys (once)

Pricing comes from eBay's Browse API, not scraping -- free, self-serve,
usually approved within a business day:

1. Go to <https://developer.ebay.com>, click **Register**, sign up with a
   business email (a personal email works fine too for this).
2. Once approved, go to **Application Keys** and create a **Production**
   keyset. Note the **App ID (Client ID)** and **Cert ID (Client Secret)**
   -- those are `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` below.
3. The keyset shows "currently disabled" until you subscribe to, or opt
   out of, eBay's marketplace account-deletion notifications (a link
   right on that page) -- **opt out**, this app never logs in as an eBay
   user or stores eBay account data, only ever the one app-level token.
4. *(Optional)* Sign up for the **eBay Partner Network**
   (<https://partnernetwork.ebay.com>) to get a Campaign ID -- if set,
   every card's listing link in the app carries your affiliate tag.

Each install of this app (yours, or anyone else's if you hand it off)
needs its own keys -- see the README for why.

## Repo private, image private -- one thing to decide

If this repo is private, the image GitHub Actions publishes inherits
"private" too by default -- which means pulling it (from TrueNAS or
anywhere else) needs registry credentials, not just the image name. Two
ways to handle that, pick whichever fits:

- **Keep the image private too**, and give TrueNAS credentials to pull it:
  add a `pull_secret`/registry auth to the Custom App config, using a
  GitHub Personal Access Token with `read:packages` scope as the password
  and your GitHub username as the username. More setup, but nothing about
  the code is exposed.
- **Make just the image public** (Packages tab on the repo, or your
  GitHub profile → Packages → package settings → Change visibility →
  Public) once it's ready to actually deploy somewhere. The source stays
  private; only the built container becomes anonymously pullable by
  anyone who has (or guesses) its exact name -- not listed anywhere
  public-facing, but not access-controlled either. **This is one-way --
  GitHub doesn't allow taking a package back to private once it's been
  made public.** `docker-compose.truenas.yaml` as written assumes this
  option (no registry auth in it).

Neither choice affects the app's own login system -- that's a separate
layer either way (see README, "Accounts & access").

## 2. Create a dataset for its data

In the TrueNAS UI: **Datasets** -> create a dataset under whichever pool
you use for app data, e.g. `apps/cardvault2`. This is where your
collection database actually lives -- keeping it as its own dataset means
it survives the container being rebuilt or the app being reinstalled.

## 3. Install via YAML

1. **Apps** -> **Discover Apps**
2. Click the **⋮** (three dots) next to **Custom App** -> **Install via YAML**
3. **Application Name**: `cardvault2` (lowercase/hyphens only -- TrueNAS
   requires this)
4. In the YAML editor, paste the contents of `docker-compose.truenas.yaml`
   from this repo, then edit three things:
   - `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` -- from step 1
   - the volume line's `YOUR_POOL` -- your actual pool name from step 2,
     e.g. `/mnt/tank/apps/cardvault2/data:/app/data`
5. **Save**

TrueNAS pulls the published image and starts the container -- no build,
first boot is just a normal Python app starting up.

## 4. Access it

`http://<truenas-ip>:8000`. For a clean local domain instead of an IP and
port, point your existing reverse proxy (Nginx Proxy Manager, Cloudflare
Tunnel, Tailscale, etc.) at that IP:8000. If that proxy terminates HTTPS,
also set `CARDVAULT_COOKIE_SECURE=true` in the app's environment so login
cookies get the `Secure` flag.

## Updating later

Every push to this repo's `main` branch rebuilds and republishes the
`:latest` image automatically (see `.github/workflows/docker-publish.yml`).
On TrueNAS: your app -> **⋮** -> **Redeploy** (or stop/start it) pulls the
new image and restarts. Your data isn't touched either way -- it's on the
dataset from step 2, not in the image.

Want to pin to a specific version instead of always tracking `:latest`?
Tag a release (`git tag v1.0.0 && git push --tags`) and reference
`ghcr.io/<owner>/cardvault2:v1.0.0` in the YAML instead.

## If pricing looks off

There's no fixed price-guide table behind this anymore -- each grade's
price is a live median across current eBay listings whose titles mention
that grade (see `backend/ebay_api.py`). A thinly-listed card, or one where
sellers word titles unusually, can come back noisier than a curated guide
would. Refreshing again later (more listings may exist) or checking the
"View listing" links directly is the fallback.

If a refresh consistently fails for a whole collection, check the
container logs for `[auto-refresh]` lines -- the most common cause is
`EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET` not being set, or eBay's free-tier
daily call limit (5,000/day, app-wide) being exceeded by a large
collection on too short a refresh interval -- see the comment above
`AUTO_REFRESH_INTERVAL_SECONDS` in `backend/main.py`.
