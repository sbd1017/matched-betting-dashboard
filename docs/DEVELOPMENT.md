# Matched betting (/bets)

Private matched-betting ledger served at **https://example.com/bets/** (FastAPI + Jinja, `bets/`).
How the app, accounting and deployment work is in `docs/OPERATIONS.md`. Read it before changing behaviour.

## Design rule: every page under /bets uses the v3 design

This applies to `/bets/` and **every page beneath it**, existing or new (dashboard, positions,
offers, free bets, accounts, ledger, calculator, imports, settings, login, errors, anything added
later). The rest of example.com is a separate site and does not need to match.

- **Source of truth:** `docs/design/README.md` (rules, tokens, components, page patterns).
- **Stylesheet:** `bets/static/design.css`. Use its tokens and classes. Don't invent new colours,
  fonts, radii or shadows; add a token or component there first if one is genuinely missing.
- **Reference screen:** `docs/design/dashboard-v3.html`, the approved dashboard mockup
  (open it in a browser). Match its look and behaviour on every new page.
- The old dark stylesheet (`app.css`) has been removed. Never reintroduce its look (dark panels,
  rounded cards, capsule pills) or a second stylesheet.

The short version of the design (details in the README):
- Glance first. The most important numbers and the things that need doing come first on every page.
- No cards. Sections are separated by space and 1px rules, not boxes with borders and shadows.
- Back (bookmaker) side is always exchange blue, lay (exchange) side is always exchange pink.
- One family, Archivo: condensed and heavy for money, normal width for text. IBM Plex Mono for odds.
- Sentence-case labels, no uppercase eyebrows, no badge-count pills, no icon on every nav item,
  no gradients or glows, no emoji.
- Light and dark themes both supported through tokens.

## Hard constraints (CSP)

The app and nginx send `default-src 'self'; script-src 'self'; style-src 'self'`. So:
- No external fonts, scripts or stylesheets (no Google Fonts, no CDNs). Fonts are self-hosted in
  `bets/static/fonts/` (see the README for the files).
- No `<style>` blocks, no `style="…"` attributes, no inline `<script>` or `onclick=`. Put CSS in
  `design.css` and JS in `bets/static/app.js`. For data-driven sizes (bar widths, chart marks) use
  SVG attributes or set `element.style` from `app.js`.
- Bump the `?v=` query on the stylesheet/script links in `base.html` when you change them.

## Data and copy rules that the design depends on

- Money is integer pence; format with the existing `fmt()`/`money()` helpers, never floats.
- Every figure shown must come from the app's services so totals reconcile. Don't hard-code numbers.
- Write labels from the user's side ("Free to bet", "Still to come"), active voice, short sentences.

## Feature map (where things live)

- `bets/issues.py`: everything that needs putting right (Issues page, dashboard, nav count, assistant), each with
  the records involved and `repairs` (named changes with parameters).
- `bets/ops.py`: named maintenance changes (edit / link / settle / void / reverse / delete) used by the assistant
  and the web app: dependency checks, permission levels, exact savepoint previews, idempotent assistant
  requests (`assistant_ops`), held changes the user confirms at /changes/<id>.
- `bets/readapi.py`: read-only JSON views (labelled balances) for the assistant and every change's before/after.
- MCP `initialize` reports serverInfo.version = plugin.json version + `+tools.<fingerprint of the tool manifest>`
  (bets_mcp/server.py `server_version`), so ChatGPT sees a new version whenever tools change. Bump plugin.json
  for user-visible releases.
- `bets_mcp/maintenance.py`: the MCP read and change tools; `python -m bets.cli audit [--apply KEY…]`: the
  historical audit (dry run unless issues are named).
- `bets/outlook.py`: money due back (temporary funding, oldest repaid first), withdrawable cash, retention,
  Still to come, money in transit, efficiency figures for the Profit page.
- `bets/undo.py`: the Undo button after an action (30 minutes, refused once records changed).
- `bets/changes.py`: the assistant's `amend` / `reverse` candidates.
- `bets/digest.py`: Telegram summary and alerts (`scripts/telegram-setup.sh`, deploy/*digest*, *alerts*).
- `bets/commission.py`: promotional commission rates with an end date (`accounts.commission_expires_at`,
  any exchange): account pages, calendar, dashboard rows at 30 / 7 days, an issue once ended, and a warning
  wherever the stored rate is used. The stored rate is never changed automatically.
- Offer terms and the offer state machine: `services.clean_terms`, `receive_reward`, `refresh_offer_progress`.
- Pages: /issues, /accounts/check, /search, /calendar, /help (plus the drawer calculator in base.html).
- Operations layer (migration 0013): `bets/plans.py` (Bet Plans: states, retention policy, recalculation with
  revisions, validation, funding planner, placement / orders / fills / atomic `record_matched_bet`, plan ↔
  position sync), `bets/outcomes.py` (outcome buckets, per-leg P&L, worst/best/locked, equal-profit hedge
  solver; the simple case uses `calc.match`), `bets/exposure.py` (a position's verified outcome scenarios and
  per-exchange liability), `bets/normalise.py` (event/market/selection keys, aliases), `bets/rewards.py`
  (expected rewards, reconciliation, offer lifecycle stage + next action), `bets/evidence.py` (screenshot
  metadata + optional file in `var/evidence/`), `bets/brief.py` (AI-friendly views, next actions, operating
  brief), `bets/ops_operations.py` (the named operations; level `planning` is new). Issue dismissals:
  `issues.dismiss / reopen / sync_dismissals` (fingerprinted, reopen when facts change). MCP tools:
  `bets_mcp/operations.py`; tests: `tests/test_operations.py`, `tests_mcp/test_operations_mcp.py`.
  Pages: /operations, /plans, /plans/<id>, /settings/retention-policy, evidence on plan and position pages.
- Offer lifecycle (migration 0015): `bets/offer_state.py` is the ONLY place offer status/stage/next action are worked
  out (`derive`) and written (`sync`, called by every service that can move an offer on, by `ops.execute` and the
  dashboard). Never write `offers.status` elsewhere; people set only complete / expired / abandoned. Reward trigger:
  `offers.reward_trigger`; bookmaker confirmation: `offers.qualified_at`. Hedging: `positions.hedge_strategy`
  (`services.set_hedge_strategy`, `hedge_state`); deliberately unhedged qualifiers are not "incomplete".
  `times.from_local_input(..., happened=True)` for times that already happened.

## Rules for changing the data model

- Every change to money goes through `bets/services.py` (audited; ledger entries superseded, never edited).
  Audit rows carry `actor` (user / assistant:<token> / assistant+user / system) and `request_id`.
- Editing existing records from the assistant or new UI controls: add a named change in `bets/ops.py` (plan with
  level, dependencies and noop detection; apply only via services), a tool in `bets_mcp/maintenance.py`
  (+ `upstream.CHANGES`), and tests through the MCP layer (`tests_mcp/test_maintenance_mcp.py`).
- A new import candidate kind needs: a normaliser, title, summary, to_raw and apply in `imports.py`, a
  schema entry in `assistant.py`, a preview in `web.kind_preview`, an MCP tool in `bets_mcp/tools.py` (+
  `upstream.CANDIDATE_TYPES`), **and a migration that adds it to the `import_candidates.kind` CHECK**
  (`test_every_candidate_kind_can_be_stored` fails otherwise). Corrections stay out of `imports.TRUSTABLE`.
- Keep `docs/assistant-handover.md` and the plugin's SKILL.md in step with the assistant's capabilities.

## Running the tests

`.venv/bin/python -m pytest tests` (web app) and `.venv-mcp/bin/python -m pytest tests_mcp` (plugin). In a
git worktree, symlink `.venv` and `.venv-mcp` from the main checkout first (they are excluded locally).

## Migration status

- [x] Design system written (`docs/design/`, `bets/static/design.css`).
- [x] Fonts self-hosted in `bets/static/fonts/` (Archivo variable + IBM Plex Mono, OFL, latin subset).
- [x] `base.html` uses `design.css` (top bar, theme toggle: `static/theme.js` + `app.js`); `design.css`,
      the fonts and `icon.svg` are in the login-free `public` set in `bets/web.py`.
- [x] Dashboard rebuilt on the v3 patterns (headline numbers, Up next with inline positions, Your money,
      Free bets, Offers, compact Recently settled).
- [x] Every other page renders in v3 through the "Page blocks" section of `design.css`
      (`.panel`, `.stats`, `.item`, forms, `.leg-card`…). `app.css` is deleted.
- [ ] Rebuild the remaining pages on the dashboard's patterns (headline numbers, `.row` lists,
      `.ladder` odds cells) instead of the generic page blocks: positions, position, offers, offer,
      free bets, accounts, account, ledger, imports, import, quick add, calculator, settings, audit.

The service runs straight from this working tree (`matched-betting.service`), so template
and static changes go live on the next request/restart. Build and check UI changes on a branch or a
second local port before they land on `main`.
