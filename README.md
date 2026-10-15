# Matched Betting Dashboard

I built this to keep track of my matched betting after a spreadsheet stopped being enough. It runs on a
Raspberry Pi at home and I use it every day.

It's a ledger, not a bot. You place your bets yourself and record them here. It never logs in to a bookmaker
or exchange and it never moves money.

## What it does

- Groups each back bet with its lay bet(s) into a position and works out what you get back whatever happens.
  That includes free bets and bets with more than one lay, like correct score hedges on a Bet Builder.
- Lay stake calculator, plus bet plans that get checked against the offer terms, the free bet and how much
  cash is free on the exchange before you place anything. If the odds move, it shows how much you'd keep.
- Keeps the books properly. Amounts are stored in pence, ledger entries are never edited (only replaced) and
  every change is logged. Profit only comes from settled bets, so deposits, withdrawals and free bet values
  never show up as profit.
- Splits exchange balances into available cash, money held for open lays, and the total.
- Follows each offer from sign up to the reward arriving and being used, and warns you before free bets
  expire.
- Tracks money borrowed from your bank account and tells you when it's safe to send it back.
- An Operations page that lists what needs doing next, and an Issues page that spots records that don't
  add up and suggests a fix.
- Profit page, calendar, search, CSV, JSON and calendar exports, dark mode, and it works fine on a phone.
- Works in pounds by default, but you can switch to euros, dollars and a few others in Settings.
- Optional ChatGPT connection through an MCP server with OAuth. ChatGPT can read your records and log bets
  from your screenshots, but anything risky waits for you to confirm it.
- Optional Telegram summary each morning.

## Built with

Python 3.11, FastAPI, Jinja and SQLite. There's very little JavaScript and no frontend framework.

## Running it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export BETS_DATA_DIR=./var BETS_PORT=8760 BETS_SECURE_COOKIES=0 BETS_PUBLIC_ORIGINS=http://127.0.0.1:8760
.venv/bin/python -m bets.cli init-login - admin
.venv/bin/python -m bets.serve
```

The `init-login` line prints a random password for the user `admin`. Then go to
http://127.0.0.1:8760/bets/ and sign in. The database is created in `./var` the first time it starts.
`BETS_SECURE_COOKIES=0` is only there because you're on plain http locally.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests
```

The MCP tests need their own virtualenv because they start the app and the MCP server and talk to both over
HTTP:

```bash
python3 -m venv .venv-mcp
.venv-mcp/bin/pip install -r requirements-mcp.txt pytest httpx
.venv-mcp/bin/python -m pytest tests_mcp
```

## Putting it on a server

It only listens on localhost and expects to sit behind nginx under `/bets/`. The `deploy` folder has systemd
units (the app and the MCP server run as separate locked-down users) and the nginx config, and
`scripts/install.sh` sets it all up. Swap `example.com` for your own domain. There's more detail in
[docs/OPERATIONS.md](docs/OPERATIONS.md), including backups and restoring.

## Updating

Everything you enter is kept in the `var` folder, which git never touches. When there's a new version, run this
from the project folder:

```bash
scripts/update.sh
```

It backs up your data, pulls the new code, installs anything new and restarts the app. The app upgrades its own
database when it starts, so your bets, free bets and history all carry over. If anything goes wrong, the backup
is in `var/backups`.

To hear about new versions, watch the repo on GitHub (Watch, then Custom, then Releases).

## Security

There's one login, with the password hashed using scrypt. Sessions are signed, every form has a CSRF token,
the Content Security Policy is strict and failed logins are rate limited. The data folder is only readable by
the app's own user and is never served. ChatGPT gets its own token, everything it sends is checked on the
server and logged, and you can make deletions and money corrections wait for your approval.

## Disclaimer

This is a personal project. It isn't financial advice, and it's up to you to stick to each bookmaker's terms.

## Licence

MIT, see [LICENSE](LICENSE).
