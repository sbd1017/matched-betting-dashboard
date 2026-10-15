# ChatGPT plugin: Matched Betting Assistant (MCP)

Talk to ChatGPT ("Add Betfred, £43.27, bookmaker") and it submits **pending candidates** to
https://example.com/bets/. Nothing is recorded until you **Confirm** (or Edit / Reject) under Imports.
It never places bets and cannot read your balances, profit, ledger or bets.

## Architecture

```
ChatGPT ──HTTPS──> Cloudflare ──tunnel──> nginx (127.0.0.1:8081)
   /bets/mcp, /bets/mcp/*, two /.well-known paths ──> matched-betting-mcp (127.0.0.1:8762, user bets-mcp)
        └── only outbound call ──> Assistant Intake API http://127.0.0.1:8760/bets/api/assistant/ (scoped token)
                                      └── import candidate ──> you confirm in /bets/ ──> normal services ──> ledger/audit
```

* `bets_mcp/` is only an adapter: every tool maps to one Assistant Intake endpoint. It runs as its own
  Unix user that **cannot read** the betting database (`var/` is 0700 `bets`). Its only outbound calls
  are the Assistant Intake API on 127.0.0.1 and ChatGPT's CIMD documents on `chatgpt.com/oauth/…` (fixed URL
  patterns in code; nothing from a request is ever fetched, so no SSRF).
* Streamable HTTP, stateless JSON responses, official MCP Python SDK (`mcp==2.3.0`, own venv `.venv-mcp`).

## Tools

| Tool | What it does | Writes? |
|---|---|---|
| `get_intake_schema` | fields, allowed values and rules for each entry type | no |
| `calculate_match` | the app's lay stake / liability / outcomes for a back+lay | no |
| `get_candidate_status` | pending / confirmed / rejected for a candidate it submitted | no |
| `submit_account_candidate` | new bookmaker / exchange / bank (+ current balance = opening capital) | pending candidate |
| `submit_balance_candidate` | current balance of an existing account (reconciliation on confirm) | pending candidate |
| `submit_transfer_candidate` | money moved; `funding_type` temporary / permanent / between_betting_accounts | pending candidate |
| `submit_position_candidate` | bets you placed: back (+ lay); lay stake calculated by the app | pending candidate |
| `submit_offer_candidate` | an offer to track | pending candidate |
| `submit_free_bet_candidate` | a free bet credited (snr / sr) | pending candidate |
| `submit_note_candidate` | a note appended to an account / offer / position on confirm | pending candidate |
| `submit_settlement_candidate` | the result of a position you placed (by number, or event + selection matching exactly one open or just-submitted position); `outcome` back_won / back_lost / void, or per-bet `back_result` / `lay_result` for dead heats and cash-outs; `settled_at` required; the app calculates every amount, reported figures are only checked | pending candidate |

**Free bets:** a position whose type is `free_bet_snr` / `free_bet_sr` uses one of your free-bet tokens. The
token is chosen when you confirm (same bookmaker, value and SNR/SR type, not linked to another bet: earliest
expiry, then oldest, then lowest id), so several pending positions never claim the same one; an explicitly
linked token wins. No suitable token = the position waits. `free_bet_already_marked_used` links a token you had
marked used by hand (historical repair; never automatic). The chain offer → token → bet → position is kept.

Every submit tool takes `dry_run` (check only) and `request_id` (retry safety). Without a `request_id`
an identical call within 15 minutes is treated as a retry, so a retried tool call never stores twice.
Errors are structured: `unknown_account`, `account_exists`, `missing_field`, `invalid_amount`,
`invalid_odds`, `invalid_funding_type`, `invalid_datetime`, `wrong_account_kind`, `not_found`,
`duplicate_conflict`, `rate_limited`, `assistant_disabled`, `unavailable`, with `field`, `value` and a
`hint`. Warnings carry codes such as `suspicious_value` and `server_calculated`.

**Cannot** (no tool exists): confirm/reject, settle, delete, void, reconcile directly, export, read
balances/bankroll/profit/ledger/positions, change settings or authentication, backups, place bets,
anything to do with bookmaker logins. Server instructions and the skill tell ChatGPT the workflow.

## Authentication (mixed auth + CIMD)

ChatGPT's builder accepts this server with **"OAuth or no authentication"** (mixed) and **CIMD** client
registration; pure OAuth and DCR were rejected at "Create as a plugin" (see git history, 2026-10-09).

* **Anonymous:** MCP `initialize` / `tools/list` (tool structure only, no user data), `get_intake_schema`
  (rules and fields) and `calculate_match` with an explicit commission. Looking up an exchange's stored
  commission by name needs the sign-in.
* **OAuth (`bets.intake`):** every tool that touches the dashboard: all `submit_*` tools and
  `get_candidate_status`. Without a valid token they return `isError` with
  `_meta["mcp/www_authenticate"]` (`Bearer resource_metadata=…, error="insufficient_scope", …`), which makes
  ChatGPT offer to connect, then retry with the token.
* Each tool declares `securitySchemes` at the top level (and in `_meta`): anonymous tools
  `[{"type":"noauth"},{"type":"oauth2","scopes":["bets.intake"]}]`, protected tools
  `[{"type":"oauth2","scopes":["bets.intake"]}]` (every tool needs an OAuth scope tag for the builder).
* A protected tool runs only with a token this server issued (issuer), for `https://example.com/bets/mcp`
  (resource/audience), unexpired, with `bets.intake`. Tokens are hashed at rest; access 1 h, refresh tokens
  rotate (re-use revokes the connection).
* **CIMD:** ChatGPT's `client_id` is the URL of its own metadata document. Only
  `https://chatgpt.com/oauth/client.json` and `https://chatgpt.com/oauth/<callback_id>/client.json` are ever
  fetched (HTTPS, no redirects, 16 KB, 5 s, cached by max-age); the document must name itself as `client_id`,
  carry no secret, allow token method `none`, list an allowed ChatGPT callback and allow the code flow. No
  registration endpoint, no client secrets. Authorization code + PKCE (S256); `resource` is bound into tokens.
* **Approving a connection** needs a one-time **connection code** made on the Pi (works once, 15 minutes),
  not your /bets/ password.
* The plugin calls the Assistant Intake API with its own token in `mcp-var/assistant-token` (0600); ChatGPT
  never sees it. The /bets/ login does not work on the MCP endpoint and plugin tokens work nowhere else.

## Commands (on the Pi)

```bash
cd /opt/matched-betting
scripts/mcp-access.sh connect-code     # code for the consent page (shown once, 15 minutes)
scripts/mcp-access.sh status           # on/off, linked, connections (never secrets)
scripts/mcp-access.sh revoke           # disconnect ChatGPT now (reconnect needs a new code)
scripts/mcp-access.sh disable          # switch the plugin off (also revokes); 'enable' to switch on
scripts/mcp-access.sh link-assistant   # give the plugin a fresh Assistant Intake token (rotates it)
sudo systemctl status matched-betting-mcp
journalctl -u matched-betting-mcp -n 50   # tool name, type, candidate id, outcome only
```

## Connect it in ChatGPT (web)

1. Open https://chatgpt.com/plugins → **+** → **Add custom MCP server**.
2. Name **Matched Betting Assistant**; description: *Conversationally add matched-betting accounts,
   transfers, offers, free bets and positions to your private dashboard as pending candidates.*
3. Connection: **Server URL** `https://example.com/bets/mcp` (not Tunnel).
4. Authentication: **OAuth or no authentication**; Registration method: **Client ID Metadata Document
   (CIMD)**; default/base scopes: leave blank.
5. **I understand and want to continue** → **Create as a plugin**, then install it from
   https://chatgpt.com/plugins?view=personal (open it → **+**).
6. In a new Work chat type `@Matched Betting Assistant`. The schema and calculator work straight away; the
   first submission asks you to **connect**. Make a code on the Pi first:
   `scripts/mcp-access.sh connect-code` → type it on the "Connect … to Matched Betting Assistant" page.

ChatGPT asks you to approve each submit tool call (write actions); you can allow it for the conversation.
After changing tools on the server: Plugins → the plugin → **Refresh**.

## Disconnect / uninstall

* ChatGPT: Plugins → Matched Betting Assistant → disconnect / uninstall (and delete the plugin).
* Pi: `scripts/mcp-access.sh revoke` (or `disable`). To remove it entirely: stop and disable
  `matched-betting-mcp`, remove the MCP locations from `deploy/nginx/bets-locations.conf`, reinstall nginx files.

## Troubleshooting

* *Can't connect / no tools*: `scripts/mcp-access.sh status` (ON? linked?), `systemctl status
  matched-betting-mcp`, then `curl -s https://example.com/.well-known/oauth-protected-resource/bets/mcp`.
  The service log shows `cimd accepted/rejected url=…` for each ChatGPT client document it checks.
* *"That code is wrong, used or expired"*: make a new code (5 wrong tries per 15 minutes per IP → wait).
* *assistant_disabled*: `scripts/mcp-access.sh link-assistant` (the Assistant Intake token was rotated
  or disabled elsewhere).
* *Cloudflare*: ChatGPT's requests come from OpenAI's servers. If Cloudflare bot protection or a WAF rule
  challenges them, connecting fails before reaching the Pi. Allow `/bets/mcp*` and the two
  `/.well-known/oauth-*/bets/mcp` paths in that rule. Nothing else needs changing; no router ports.
* *Secure MCP Tunnel* (OpenAI's outbound tunnel client) is an alternative for servers that must stay off
  the internet; it needs an OpenAI Platform organisation, API key with Tunnels permissions and a running
  `tunnel-client`. The public endpoint behind the existing Cloudflare Tunnel already avoids open ports,
  so it is not used here.

## Limits

20 tool calls/minute and 400/day per connection; Assistant Intake allows 60 submissions/hour and 100
pending. nginx: 60 requests/minute to `/bets/mcp`, 12/minute to the OAuth paths per IP; 32 KB bodies.
