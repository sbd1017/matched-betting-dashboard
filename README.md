# Matched Betting Dashboard

A self-hosted dashboard for **matched betting**: it keeps an exact, auditable ledger of the bets, free bets,
offers and money movements you make yourself, works out what every position will return, and tells you what
to do next. It runs comfortably on a Raspberry Pi.

It **records** bets; it never places them, never logs in to bookmakers or exchanges, and never moves money.

> This is a personal project shared as-is. It is not financial advice, and you are responsible for following
> the rules of every bookmaker and exchange you use. Gamble responsibly.

## What it does

- **Positions** — back and lay bets grouped as one matched position (qualifiers, SNR/SR free bets, multi-lay
  "correct score" and Bet Builder hedges), with the result for every outcome worked out on the server.
- **Calculator and bet plans** — lay stake, liability, qualifying loss and free-bet retention; plans that are
  validated against the offer terms, the free bet and the exchange's available funds before you place anything;
  price changes are tracked with your retention thresholds.
- **Exact accounting** — integer pence, an append-only ledger (entries are superseded, never edited), every
  change audited. Realised profit only ever comes from settled bets: deposits, withdrawals, temporary funding,
  free-bet face value and open liability never count as profit.
- **Exchange balances done properly** — available cash, reserved lay liability and total funds are kept apart,
  including unmatched orders and partial fills.
- **Offers and free bets** — one lifecycle per offer (opt-in, deposit, qualifier, reward due, reward received,
  converting, complete), expected rewards reconciled against the free bets that actually arrive, expiry tracking.
- **Funding** — temporary funding from a bank (and when it can go back), bankroll contributions and withdrawals.
- **What should I do now?** — an Operations page and next-actions list: expiring free bets, unhedged bets,
  finished events to settle, overdue rewards, money that can be returned.
- **Issues** — an integrity checker that finds inconsistent records and offers named, audited repairs.
- **Profit, calendar, search, CSV/JSON/iCalendar exports**, light and dark themes, works on a phone.
- **Optional AI assistant (ChatGPT) integration** — an MCP server with OAuth 2.1 (PKCE) so an assistant can read
  your records, plan bets and record what you placed from your screenshots, within permission levels you set.
  New entries and risky corrections wait for your confirmation; the assistant can never approve its own changes.
- **Optional Telegram** morning digest and alerts.

## Tech

Python 3.11+, FastAPI, Jinja templates, SQLite. No JavaScript framework (a little vanilla JS), a strict Content
Security Policy, self-hosted fonts. The MCP server is a separate process using the official `mcp` SDK.

```
bets/            the web app: services (all money changes), ledger metrics, issues, plans, offers, pages
bets_mcp/        the optional MCP server for AI assistants (OAuth, tools, rate limits)
plugin/          the assistant plugin manifest and its instructions
deploy/          example systemd units and nginx configuration
scripts/         install, backup, restore, password and token helpers
tests/           app tests          tests_mcp/   end-to-end MCP tests (OAuth -> MCP -> API -> services)
docs/            OPERATIONS.md (how a real deployment runs), DEVELOPMENT.md, design system, assistant docs
```

## Quick start (local)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export BETS_DATA_DIR=./var BETS_PORT=8760 BETS_SECURE_COOKIES=0 BETS_PUBLIC_ORIGINS=http://127.0.0.1:8760
.venv/bin/python -m bets.cli init-login - admin    # prints a random password for user "admin"
.venv/bin/python -m bets.serve
```

Open <http://127.0.0.1:8760/bets/> and sign in. The database is created in `./var` on first start
(`BETS_SECURE_COOKIES=0` is only for plain-http local use).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests

# MCP tests (a second virtualenv; they start the app and the MCP server and talk to them over HTTP)
python3 -m venv .venv-mcp
.venv-mcp/bin/pip install -r requirements-mcp.txt pytest httpx
.venv-mcp/bin/python -m pytest tests_mcp
```

## Deploying

The app listens on loopback only and is meant to sit behind a reverse proxy under `/bets/`. The `deploy/`
folder has hardened systemd units (separate unprivileged users for the app and the MCP server) and nginx
configuration; `scripts/install.sh` installs them. Replace `example.com` with your domain throughout.
[`docs/OPERATIONS.md`](docs/OPERATIONS.md) describes how a real deployment is run: login, backups, restores,
the assistant integration and day-to-day use.

## Security

- One login, scrypt-hashed; sessions signed with a per-install secret; CSRF tokens and origin checks on every
  write; strict CSP; rate-limited login.
- The data directory is private to the service user; backups and data are never served.
- The assistant uses its own scoped token or OAuth; every assistant change is validated server-side, idempotent
  and audited, and deletions or financial corrections can be set to wait for you.

## Licence

[MIT](LICENSE)
