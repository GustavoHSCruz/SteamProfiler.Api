# Contributing

Thanks for helping improve SteamProfiler.

## Before opening a change

- Search existing issues and pull requests.
- Keep changes focused. Discuss large API, persistence, privacy, or deployment
  changes in an issue first.
- Never include credentials, production databases, address hashes, access
  logs, or private Steam profile data in an issue, fixture, or commit.
- Preserve the standard-library-only runtime unless a new dependency has a
  clear operational benefit and has been discussed first.
- The Python service is frozen at `v1.0.0` and takes security fixes only. New
  behavior and behavior fixes belong in `v2/` (.NET 10), following
  [docs/v2/PLANO.md](docs/v2/PLANO.md): a handler per use case, rules in the
  aggregate, and no reference from `2.Application` to `4.Infrastructure`.

## Local workflow

1. Copy `.env.example` to `.env` and use your own Steam API key.
2. Make the change and add or update tests under `tests/`.
3. Run `./check.sh`.
4. Explain user-visible behavior, privacy implications, and operational
   changes in the pull request.

Tests must not depend on the network. Mock Steam and other upstream responses
with local fixtures so CI remains deterministic.

## Style

Follow the surrounding code. Prefer small standard-library modules, explicit
failure modes, bounded caches, and comments that explain operational or privacy
tradeoffs. User-facing strings belong in the frontend dictionary when the
frontend renders them.

## Pull requests

By contributing, you agree that your contribution is licensed under the MIT
License. Maintainers may ask for changes when a patch affects API-key spend,
retention, abuse controls, accessibility, or compatibility with the frontend.
