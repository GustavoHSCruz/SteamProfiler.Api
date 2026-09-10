# SteamProfiler API

[![CI](https://github.com/GustavoHSCruz/SteamProfiler.Api/actions/workflows/ci.yml/badge.svg)](https://github.com/GustavoHSCruz/SteamProfiler.Api/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

<a href="https://ko-fi.com/gordziilla"><img src="https://storage.ko-fi.com/cdn/kofi3.png?v=3" alt="Support me on Ko-fi" height="36"></a>

The server-side half of [steamprofiler.org](https://steamprofiler.org). It reads
public Steam profiles, enriches them with store and community data, caches the
results, and serves the
[SteamProfiler frontend](https://github.com/GustavoHSCruz/SteamProfiler.Front).

The application uses only Python's standard library: no framework, package
manager, or runtime dependency. You provide your own Steam Web API key and run
your own instance.

## Features

- Profile, library, achievement, friend, card, wishlist, Workshop, and game
  catalogue readers.
- Embeddable renderers: a link-preview PNG drawn without an image library, and
  self-contained SVG charts, banners and badges for a README, a forum or a blog.
- Bounded in-memory caches for profile data and SQLite caches for shared public
  metadata.
- Per-client rate limits, scanner traps, and a daily Steam-key budget.
- Privacy-aware logs and salted address identifiers instead of raw IP storage.
- Optional owner-only moderation and blog administration panel.
- Docker Compose deployment with nginx and an unprivileged API container.

## Quick start

You need Git, Docker with Compose v2, and a
[Steam Web API key](https://steamcommunity.com/dev/apikey).

```sh
git clone https://github.com/GustavoHSCruz/SteamProfiler.Api.git steamprofiler-api
git clone https://github.com/GustavoHSCruz/SteamProfiler.Front.git steamprofiler-front
cd steamprofiler-api
cp .env.example .env
```

Set your instance identity and API key in `.env`:

```dotenv
STEAM_API_KEY=your-key
STEAM_ID=your-steamid64
STEAM_VANITY=your-vanity-name
```

Start the public services:

```sh
docker compose up --build -d
curl --fail http://127.0.0.1:16200/healthz
```

Open <http://127.0.0.1:16200>. The default bind is loopback-only. Set
`HTTP_BIND=0.0.0.0` only when you deliberately want remote access, preferably
behind TLS. Set `FRONTEND_DIR` if the frontend is not checked out beside the
API.

## Run without Docker

Python 3.12 or newer is recommended. There is nothing to install:

```sh
set -a
. ./.env
set +a
python3 api.py
```

The server listens on `PORT` (8000 by default) and writes persistent state to
`DATA_DIR` (`./data` outside a container).

## Configuration

[`.env.example`](.env.example) documents every setting and its default,
including cache lifetimes, upstream pacing, offline development modes, rate
limits, donations, privacy salts, and the optional Ollama integration.

Never commit `.env`. For a public deployment, generate separate random values
for `ADMIN_TOKEN`, `IP_SALT`, and `CENSUS_SEED`:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## Architecture

```text
browser -> nginx -> guard.py -> api.py -> Steam / OpenDota
             |                    |
             |                    +-> in-memory profile cache
             +-> static frontend  +-> SQLite public-data caches
```

The main modules are `fetch.py` for Steam profiles, `meta.py` for store data,
`cards.py` and `inv.py` for Community Market data, `fx.py` for exchange rates,
`houses.py` for the publisher and developer index, `census.py` for aggregate
traffic counts, and `og.py` and `embed.py` for the two kinds of picture this
service draws - the link preview, and the charts, banners and badges meant to
be pasted somewhere else. Everything `embed.py` produces is self-contained: key
art and avatars are cached by `art.py` and inlined as data URIs, because a file
that fetches anything at render time is a file that draws differently, or not
at all, wherever it was pasted.

`houses.py` is worth one paragraph because its source is incomplete and the
module is shaped around that. It fills weekly from SteamSpy's `request=all`,
which is ordered by owners and stops: measured on 2026-09-09 it ended at page
86 with 82,493 of the 175,162 apps Steam lists. The tail it drops is where a
studio has one game and no other way to be found. Two things close the gap and
both write to `house_learned`, the one table the weekly rebuild merges rather
than replaces: a paced per-app walk over the catalogue snapshot, and the store
detail pass handing over the two strings it already read for any game somebody
opened.

The service was built for a small independent site. Routes and payloads may
evolve with its frontend; it is not currently a versioned third-party API.

The exception is `GET /api/player?appid=<n>`, the deliberately small,
cross-origin contract published for
[SteamProfiler Player](https://github.com/GustavoHSCruz/SteamProfiler.Player).
Its response carries `version: 1` and only the title, poster, storefront link,
attribution and browser media sources for one highlighted trailer. A cold app
answers `state: pending` while its store record is queued; a known app without
a trailer answers `state: absent`; only `state: ready` carries media. This
endpoint never exposes a Steam Web API key and sends no credentials to Steam.

`GET /api/companion?appid=<n>&l=<language>` is the other versioned public
contract. It powers the Steam store panel from
[SteamProfiler Companion](https://github.com/GustavoHSCruz/SteamProfiler.Companion)
with catalogue identity, lifetime reviews and a bounded sample of the latest
reviews, current players, recent official activity and the same trailer
envelope. It deliberately sends only the latest news title instead of the news
feed. Both public contracts permit cross-origin reads and carry `version: 1`.

`GET /api/companion/profile?appid=<n>&id=<steamid64>` is the Companion's
opt-in personal supplement. It returns only hours, last-played date and compact
achievement progress for that game. Unlike the two public game contracts, it
does not publish CORS access to arbitrary websites; the extension reaches it
through its declared `steamprofiler.org/api/*` host permission.

## Admin panel

The panel belongs to this repository, not to the public frontend: its browser
assets, authentication server, and privileged API routes evolve as one unit.
It only reuses the frontend's shared styles, dictionary, and fonts. Publishing
the source does not publish the panel itself.

The optional admin service is absent from a normal Compose startup, is blocked
by the public nginx listener, and binds to loopback by default:

```sh
docker compose run --rm admin python /app/admin/setup-cred.py
docker compose --profile admin up --build -d
```

Prefer an SSH tunnel to publishing its port. If you change `ADMIN_BIND`, also
keep `ADMIN_ALLOW_IPS` narrow and configure `ADMIN_TOKEN`.

## Development

Run the same validation used by CI:

```sh
./check.sh
```

It checks Python and JavaScript syntax, runs 59 unit tests, audits the release
tree for common secret leaks, and validates nginx and Docker Compose.

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before a
pull request and report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).

SteamProfiler is an independent hobby project and is not affiliated with,
endorsed by, or connected to Valve Corporation. Steam and the Steam logo are
trademarks of Valve Corporation. Game names and art belong to their respective
owners.

Built with AI assistance, reviewed and shipped by a person.
