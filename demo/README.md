# Public demo

A copy of the app filled with about 18 months of made-up matched betting, open to anyone without signing in, and
rebuilt every night. It's what runs at https://mubar.uk/bets/.

## What's in it

`demo/seed.py` runs the history forward in time and records each step through the app's own code (`bets.services`
and `bets.plans`) with the app's clock set to when it happened. So every ledger entry, audit row and offer status is
what the app itself would have written, and every page balances.

- Real UK bookmakers (Bet365, Sky Bet, William Hill, Paddy Power, Betfred, Coral, Ladbrokes, BetVictor and more)
  and exchanges with their usual commission: Smarkets 2%, Betfair Exchange 5%, Matchbook 1.5%. Betdaq was opened
  for a 0% commission offer that has now ended, so the Issues page asks you to check the rate.
- Sign-up offers first, then weekly reloads, Bet Builders covered with correct score lays (1-1 and 2-2, or 2-1),
  and festival offers (Cheltenham, the Grand National, Royal Ascot, the 2026 World Cup) when the dates fall inside
  the window.
- Real Premier League, Championship and Champions League clubs, World Cup teams and tennis players, and real
  racecourses and race names, on the days and times those fixtures are normally played. The horses' names are made
  up. Prices come from each selection's chance of winning, so back and lay odds sit close together the way an
  oddsmatcher finds them. Qualifying losses are mostly 5p to £1 (Bet Builders about £1.10), and free bets keep
  around 78%.
- Deposits from the hub bank, withdrawals that take a day or two to arrive, profit taken out as profit withdrawals,
  money borrowed from a second bank for busy weeks (one loan still outstanding and due soon), fortnightly balance
  checks, and a bookmaker that restricted the account.
- Left for you to find: a few open positions and plans, free bets that expire soon, last night's bet that nobody
  settled, a reward that hasn't arrived, a withdrawal that's late, and a balance check that's 50p out.

About 575 offers and 1,150 positions, and a little under £3,000 profit.

It's deterministic: the same `--today` always gives the same records. Fixtures are fixed per date, and dates are
worked out back from today, so a nightly rebuild keeps the demo current.

```bash
.venv/bin/python -m demo.seed --data-dir /tmp/bets-demo --today 2026-10-20
```

It only ever seeds a new, empty folder.

## Running it

`demo/serve.py` runs the app unchanged, but wrapped:

- No sign-in: every visitor gets a session as the user `demo`. CSRF tokens and the same-origin checks still apply
  to every change.
- Nothing reaches outside. The ChatGPT/MCP paths and the Assistant Intake API answer 404, and their settings are
  hidden. File uploads and the currency switch are refused, because everyone shares one database. Don't run the MCP
  server, the Telegram summary or the backup timers for a demo.
- A banner on every page says the records are made up and reset at 4am.
- It won't start on a database that `demo.seed` didn't build, so it can't open a real ledger without a sign-in.

```bash
BETS_DATA_DIR=/tmp/bets-demo BETS_PORT=8911 BETS_SECURE_COOKIES=0 .venv/bin/python -m demo.serve
```

Then open http://localhost:8911/bets/. The app lives under `/bets/`, so a proxy in front of it keeps the path
(`proxy_pass http://127.0.0.1:8911;` in a `location /bets/`), the same as a normal install.

## On a server

`sudo demo/deploy/install.sh` installs the committed code into `/opt/demos/bets` with its own virtualenv. The
records go in `/srv/demos/bets/var`, owned by an unprivileged `demo` user. `demo-bets.service` listens on
127.0.0.1:8911, with no outgoing network access and no access to home folders. The nightly reset runs
`demo/deploy/reset` at 04:00 UK time with the app stopped. It builds the new records first and swaps them in only
if that worked.
