# Matched betting

A private ledger for matched betting at **https://example.com/bets/** (its own service, database and login). It **records** bets you place
yourself; it never places bets, logs in to bookmakers or touches bookmaker/exchange accounts.

| | |
|---|---|
| Project | `/opt/matched-betting` |
| Service | `matched-betting.service` (user `bets`), `127.0.0.1:8760` only |
| Public URL | `https://example.com/bets/` (Cloudflare Tunnel → nginx → app) |
| Data | `var/` (0700, `bets`): `bets.sqlite3`, `auth.json` (password hash), `secret.key`, `assistant.json` (assistant token hash), `backups/` |
| Backups | daily `matched-betting-backup.timer` → `var/backups/bets-*.sqlite3` (newest 30) |

## Using it

* **Quick Add** (the + button): bookmaker, exchange, event, selection, back stake/odds, lay odds and
  commission. The lay stake, liability and both outcomes are calculated as you type; you review
  them, then save. Leave the lay empty to save the back now and add the lay later (the position
  shows as *incomplete* until then). Free bets: choose one from the list (SNR/SR handled).
* **Positions**: one per betting opportunity, holding any number of bets (back, lay, partial lays,
  free bets). Settle with one tap (*back selection won / lost / void*) or per bet for dead heats,
  cash-outs and other odd settlements. Edits never duplicate money (see Ledger).
* **Offers** and **Free bets**: status, deadlines (Add to Calendar), rewards and realised value.
* **Accounts**: balances, liability held, available money; record deposits/withdrawals as
  *transfers*; **Reconcile** against the real balance (difference becomes a visible entry).
* **Ledger**: every money movement, filterable, with running balance per account and CSV export.
* **Profit** (`/bets/profit`): realised profit by week, month, year and all time (by settlement date, UK time),
  each period drilling into the bets and rewards that made it; profit by bookmaker, offer and type;
  winnings retained or taken out, cash unlocked now, and an optional winnings plan (figures only).
* **Calculator**: qualifying bets and SNR/SR free bets.
* **Settings**: add accounts and starting balances, exports, audit trail.

## Day to day

* **Dashboard → Up next**: what needs you (started bets with Back won / Back lost / Void, expiring free bets, money
  due back to the bank with any shortfall, rewards to confirm with "It arrived", withdrawals on their way, idle cash
  to withdraw, issues), then kick-offs and deadlines by day. "Still to come" values free bets at your own retention.
* **Issues**: everything that needs putting right, each linked to where it is fixed. **Accounts → Check balances**:
  tick what matches or type the real balance, for every account at once.
* **Undo**: the message after settling, adding free bets or a reward, a transfer, an entry or balance adjustments
  has an Undo button for 30 minutes. Bets can be moved between positions, split off or merged (position page).
* **Offers** with terms (qualifying stake, minimum odds, reward count × value, SNR/SR/cash, expiry days) pre-fill
  Quick add, warn when a bet won't qualify, create the reward's free bets on "It arrived" and move on by themselves.
* **Search** (⌘K or /), **Calendar**, **Help** (glossary), calculator drawer (C), **Same again** on a position.
* **Telegram** (optional): `scripts/telegram-setup.sh`, then enable `matched-betting-digest.timer` and
  `matched-betting-alerts.timer`. Only those units may reach the internet (api.telegram.org).
* Assistants: see `docs/assistant-handover.md` for every entry type, including corrections (`amend`) and undos
  (`reverse`), which always wait for your confirmation.

## Accounting rules

Money is integer pence everywhere (no floating point). Odds/commission use exact decimals and
round to the penny once, half-up.

* **Transfers are never profit or loss.** A bank account (e.g. Lloyds) is a *funding source*, not
  bankroll. Bank → bookmaker/exchange defaults to **temporary funding**; bookmaker/exchange → bank
  defaults to a **return of temporary funding**, and anything above what is outstanding from that
  bank counts as a **bankroll withdrawal**. Choose *bankroll contribution* for money added long-term.
  Example: Lloyds → Smarkets £100, a lay settles for +£2.40 net, Smarkets → Lloyds £102.40:
  profit £2.40; temporary funding outstanding £0; £2.40 withdrawn from bankroll.
* **Profit withdrawal** (betting → bank, chosen explicitly): winnings taken out. Still a transfer, so
  realised profit never changes; it reduces *retained* profit instead of your capital, and can't exceed
  the retained profit. Earlier withdrawals keep the classification they were recorded with.
* **Opening balances** (money already at a bookmaker when you start) are capital, not profit.
* **Stakes** leave the bookmaker balance when placed and count towards profit only once the bet
  settles; until then they are "tied up". **Lay liability** is held exposure, not a cash movement.
* **Profit** = winnings, lay results, commission, refunds, free-bet winnings, cash rewards/bonuses,
  adjustments and reconciliations — plus the stakes of settled bets.
* **Free bets** are tokens, not cash: no balance change until they win.
* System entries have a posting key; changing a bet **supersedes** its entry (kept, linked,
  audited) instead of adding a second one. Manual entries are *reversed*, never deleted.
  Removing a bet/position withdraws its entries; everything stays in the history and audit trail.
* Dashboard identity (checked live): betting balances = your capital + temporary funding
  outstanding + retained profit (realised − profit withdrawn) − unsettled stakes.

## Imports and the Assistant Intake API (ChatGPT)

Every input uses one pipeline: *input → normalise/validate → preview → you confirm → the same
services as the forms*. Candidates wait under **Imports** as *pending* with **Confirm / Edit / Reject**;
nothing reaches the ledger, balances or history until you confirm. Confirming re-validates and the app
recalculates every figure. Candidate types: `position`, `account`, `balance` (current balance of an
existing account → a visible reconciliation on confirm), `transfer`, `offer`, `free_bet`, `note`.

**Assistant Intake** (`https://example.com/bets/api/assistant/`) is for a conversational assistant such as a
custom GPT. It uses its **own token** (never your /bets login), sent as `Authorization: Bearer …`:

| | |
|---|---|
| `GET /schema` | types, fields and rules (what to ask the user) |
| `GET /openapi.json` | OpenAPI 3.1 for ChatGPT Actions (same as `scripts/assistant-token.sh openapi`) |
| `POST /candidates/{type}` | submit; `dry_run: true` checks without storing; `idempotency_key` prevents duplicates on retry |
| `GET /candidates/{id}` | pending / confirmed / rejected (only candidates the assistant sent) |
| `POST /calculate` | the app's lay stake, liability and outcomes (nothing stored) |

The token can do **only** that. It cannot confirm, settle, delete, void, reconcile directly, export, or read
balances, profit, the ledger or history; every other /bets path ignores it. Responses carry only what was
sent plus the app's calculations on it (it can learn whether a named account/offer exists, never figures).
A lay sent without a stake gets the app's calculated stake. Limits: 60 submissions/hour, 100 pending,
16 KB per request; failed tokens are limited per IP (a valid token is never locked out); nginx rate-limits too.

Token (stored only as a SHA-256 hash in `var/assistant.json`; never logged, never in a URL):

```bash
scripts/assistant-token.sh create     # first token -> ~/.config/matched-betting-assistant-token.txt (0600)
scripts/assistant-token.sh rotate     # new token; the old one stops working immediately (--print: terminal)
scripts/assistant-token.sh disable    # off now (also: Settings -> Disable assistant access)
scripts/assistant-token.sh status     # on/off and the token's short id, never the token
```

Setting up the GPT (ChatGPT → My GPTs → Create → Configure → Actions): import the schema from
`scripts/assistant-token.sh openapi`; Authentication → **API key**, Auth type **Bearer**, paste the token;
then move the token to your password manager and delete the file. Suggested instructions:

> You help me record matched-betting activity in my app. I place every bet myself; never imply you placed or
> changed anything. When I mention an account, transfer, offer, free bet or bets I placed, ask for anything
> required that I haven't said (for money from my bank: temporary or permanent?), then submit it as a
> candidate and tell me it is waiting for my confirmation at the link. Never calculate lay stakes, liability or
> profit yourself: use calculateLay or leave the lay stake out and repeat the app's figures. Use the same
> idempotency_key if you retry.

`POST /bets/api/import` (signed-in session + `X-CSRF-Token`) remains for other tools (Shortcut/CSV/OCR).

### ChatGPT plugin (MCP)

ChatGPT connects through **Matched Betting Assistant**, a small MCP server (`bets_mcp/`, service
`matched-betting-mcp`, 127.0.0.1:8762, public `https://example.com/bets/mcp`; mixed auth: tool listing,
schema and calculator anonymous, everything touching the dashboard needs OAuth via CIMD + a one-time
connection code from `scripts/mcp-access.sh`). It holds the Assistant Intake token and only calls this
API. Setup, tools, scopes and troubleshooting: [docs/chatgpt-plugin.md](docs/chatgpt-plugin.md). Its tests:
`.venv-mcp/bin/python -m pytest tests_mcp`.

## Login

The app has its own username/password (not the shared private-apps login).

* First install created a random password in `~/.config/matched-betting-login.txt` (0600).
  Move it to your password manager, then delete that file.
* Change it: `ssh -t your-server /opt/matched-betting/scripts/set-password.sh`
  (prompts; nothing is echoed or logged). To sign out every device as well, delete `var/secret.key`
  (as root) and restart the service.
* Sessions: HttpOnly, Secure, SameSite=Strict cookie scoped to `/bets`, 14 days. Failed logins are
  limited per IP and overall; nginx also limits login attempts.

## Operations

```bash
sudo scripts/install.sh                       # install/refresh (venv, migrations, units)
sudo systemctl status matched-betting
journalctl -u matched-betting -n 50
scripts/backup.sh                             # backup now
sudo scripts/restore.sh bets-20261008T034000Z.sqlite3   # restore (current DB kept aside)
.venv/bin/python -m pytest                    # tests
```

Schema changes are numbered SQL files in `bets/migrations/`, applied automatically in order and
recorded in `schema_migrations`.

**nginx**: `deploy/nginx/` is the source of truth, installed as `/etc/nginx/conf.d/20-matched-betting.conf`
and `/etc/nginx/snippets/matched-betting*.conf`, included from the example.com server block. After
editing: copy, `sudo nginx -t`, `sudo systemctl reload nginx`. `tests/test_public_bets.py` runs a
throwaway nginx with these files and checks the installed copies match.

## Privacy

Nothing from this app appears in any other page or API. The only
public trace is the existence of `/bets/`, which shows a login page. Backups and data are never
served by nginx. Never store bookmaker passwords in notes.
