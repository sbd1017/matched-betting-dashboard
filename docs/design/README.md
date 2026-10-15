# /bets design system (v3)

Every page under https://example.com/bets/ uses this design. The stylesheet is
`bets/static/design.css`. The approved reference screen is `dashboard-v3.html` in this folder
(open it in a browser; it uses sample data and loads Google Fonts, which the live app can't).

## 1. Principles

1. **Glance first.** Each page opens with the few numbers or items that matter most, readable in
   two seconds. Detail comes after. If something needs doing, it's near the top with a button.
2. **No cards.** Sections are separated by space and 1px rules (`--rule`), with a 1px
   `--rule-strong` line under a group heading. No boxes with border + radius + shadow around content.
3. **The exchange colour language.** Back / bookmaker side is blue (`--back`), lay / exchange side
   is pink (`--lay`). Use them for odds cells, inputs, money blocks and chips, and for nothing else.
   Striped (hatched) versions mean money that is held or tied up.
4. **Semantic colour is separate.** Green `--gain` = profit, red `--loss` = loss or money about to be
   lost (expiry), amber `--warn` = due soon or borrowed money. Red fills (`.stat.alert`, `.btn.red`)
   are only for something that will cost money if ignored.
5. **Every number reconciles.** Figures come from the app's services, in pence, formatted with
   `fmt()` / `money()`. Never hard-code or approximate a figure in a template.
6. **Say what to do.** Rows carry their own action ("Find a match", "Review", "Mark withdrawn").
   Labels name things the way a matched bettor would ("Free to bet", "Still to come", "Held as liability").

## 2. Don'ts (these make it look generic or AI-made)

- Rounded cards with shadows; a grid of identical tiles; glassmorphism; gradients or glows.
- Uppercase letter-spaced eyebrows; badge-count pills next to every heading; "View all →" on every
  section (make the section title the link instead).
- An icon beside every nav item or heading; emoji; decorative illustrations or stock imagery.
- Generic bookmaker logos made of a letter in a coloured square.
- New fonts, colours, radii or shadows that aren't tokens in `design.css`.
- Showing the same item in several widgets on one page.

## 3. Tokens

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | #FFFFFF | #0F1012 | page |
| `--sunk` | #F3F4F5 | #18191C | inputs, hover |
| `--ink` / `--ink-2` / `--ink-3` | #111316 / #53585F / #8E939A | #EEEFF1 / #A4A8AE / #6E737A | text levels |
| `--rule` / `--rule-strong` | #E3E5E8 / #111316 | #26282C / #EEEFF1 | dividers / group heading line |
| `--back` / `--back-deep` / `--back-wash` | #A6D8FF / #0A3152 / #E6F3FF | #9DD3FF / #062540 / #132535 | back side fill / text on fill / stripe |
| `--lay` / `--lay-deep` / `--lay-wash` | #FAC9D4 / #5A1427 / #FDECEF | #F8BFCD / #4B0F20 / #2E1820 | lay side |
| `--gain` `--loss` `--warn` | #0A7A4F #D0303A #B76E00 | #45C78B #FF6B76 #F0A73A | semantic |
| `--loss-wash` `--warn-wash` | #FDECEC #FFF4E0 | #2C1517 #2A2010 | tinted backgrounds for alerts |

Theme: follows the system by default; `data-theme="light|dark"` on `<html>` overrides it (toggle in
`app.js`, remembered in `localStorage`). Change `<meta name="color-scheme">` in `base.html` to `light dark`.

## 4. Type

One family, **Archivo** (variable: weight 400–800, width 62–125%), plus **IBM Plex Mono** for odds,
times and small counts.

| Role | Setting |
|---|---|
| Hero money (`.stat.hero .v`) | 800, width 66%, 64–104px |
| Money figures (`.stat .v`, `.fig`, `.total b`) | 800, width 66–70%, 30–52px |
| Page title (`.page-head h1`) | 800, width 72%, 34px |
| Section title (`.sh h2`) | 700, width 80%, 22px |
| Row title | 600, 15px |
| Body / secondary | 400, 13–14px, `--ink-2` |
| Odds, times, counts | IBM Plex Mono 500–600 |

Pence are shown smaller and in `--ink-3` on hero figures (`<span class="p">.43</span>`).

### Fonts (self-hosted, required by CSP)

These live in `bets/static/fonts/` with their licences (`OFL-*.txt`, SIL Open Font License 1.1):

- `Archivo-Variable.woff2`: Archivo variable font, `wght` 400–800 and `wdth` 62–125%.
- `IBMPlexMono-Regular.woff2`, `IBMPlexMono-Medium.woff2`, `IBMPlexMono-SemiBold.woff2`.

They are the Google Fonts **latin** subset (downloaded 9 Oct 2026). It covers £, €, ×, the true
minus sign and the usual punctuation; anything outside it (for example ≈) falls back to the system
font. Upgrading means replacing the files with the same names; nothing else changes.

## 5. Layout

- **Shell:** sticky top bar (`.bar`): wordmark (`.wm`, the blue/pink split square), text nav with
  counts as `<sup>` (`.hot` for counts that need attention), then clock, theme, Calculator (key C)
  and the primary **New** / Quick Add button. No sidebar and no bottom tab bar. On phones the
  nav wraps to its own scrolling row.
- **Content:** `.wrap` (max 1440px, 32px gutters, 16px on phones). Sections are `<section>` with a top
  rule; two-column areas use `.cols` (7:5) or `.cols.even`; they stack under 1080px.
- **Dashboard order:** three headline numbers (`.status.n3`: profit, free-bet expiry or free bets held,
  available to bet), then **Up next** + Free bets (left) beside Your money + a one-line ledger status with
  the 3 latest movements (right), then Offers, then Recently settled as a compact table. Up next is one
  chronological list grouped Needs you / Today / Tomorrow / This week / Next week / Later; open positions
  sit in it with their `.ladder` (back blue, lay pink, liability and both outcomes), and free bets of one
  bookmaker expiring at the same moment are one row. Phones stack it as headline, Up next, Free bets,
  Your money, then the rest. The dashboard uses the denser `.dash` spacing.
- **List pages** (positions, offers, free bets, ledger…): `.page-head` (title + primary action),
  an optional `.status.n3` of that page's 2–3 key numbers, `.tabs` for filters, then rows or a table.
- **Detail pages** (a position, an offer, an account): `.crumbs`, `.page-head`, a `.status` with the
  outcome numbers, then the bets as `.ladder` cells, then history as a table.

## 6. Components (class → use)

| Component | Markup sketch | Notes |
|---|---|---|
| Headline numbers | `.status > .stat(.hero / .alert) > .k + .v + .s` | max 4; `.alert` only for imminent loss |
| Group heading | `.grp` + optional `<i>` date + `.cnt` | 1px strong rule underneath |
| Row | `.row > .kind(.k-red/.k-amb/.k-ink/.k-blue/.k-green) + div(h3,p) + .ac(.worth, .btn)` | `<a class="row">` when the whole row opens something; `.row.nokind` without the time column; `.row.done` after an action |
| Ladder | `.ladder > .odd.back / .odd.lay / .odd.free + .outc` | odds in mono; `.outc` shows the result for each outcome |
| Odds chips | `.cb` (back) `.cl` (lay) | inside tables |
| Money split | `.total`, `.loanbar`, `.blocks > .blk.b-lay/.b-lay-h/.b-back/.b-back-h`, `.mlist > .mrow` | always pair the bar with the labelled list |
| Free bet | `.fb > .amt + div(h3, p, .drain) + .worth` | `.drain` shows time left before expiry |
| Offer | `.offer > div + .steps(.d/.c) + .st + .v` | steps: Qualify, Qualifier settles, Free bets arrive, Free bets used |
| Chart | `<svg class="chart">` | step line for cumulative profit, bars for each bet, dashed projection |
| Small figures | `.kpis` | 4 across, 2 on phones |
| Tabs / range | `.tabs`, `.range` | mark the current one with `aria-current` or `aria-pressed` |
| Table | `.tw > table`; `.rt` right-aligns numbers | heading rule is `--rule-strong` |
| Inputs | `.fields > .fld.b / .fld.l / .fld` (big mono numbers), `.field` (ordinary labelled input) | back inputs blue, lay inputs pink |
| Calculator result | `.out > .main + .kv…` | |
| Buttons | `.btn`, `.btn.k` (primary), `.btn.red`, `.btn.s`, `.btn.danger`, `.ib` (icon only) | one `.btn.k` per row at most |
| Messages | `.flash.ok/.err/.warn`, `.toast` | |
| Drawer | `.scrim` + `aside.drawer` (`.on` to open) | calculator and quick add |
| Empty state | `.empty > b + text + .btn` | say what will appear and how to add the first one |

### Components added with the operational pages

| Component | Where | Notes |
|---|---|---|
| `settle_buttons(pid, next)` macro | dashboard, issues, positions | Back won / Back lost / Void for a started position; returns to `next` |
| `.settle-big` | position page | three settle buttons that show what each outcome pays |
| `.leg` / `.leg-head` | position page | one bet: odds cell, details, result; its forms sit in `details` underneath |
| `bet_ladder(p)` macro | positions list | a position's odds cells and the result either way |
| `.fbrow` + `.drain` | free bets list | big face value; expiry bar (share of life left, set from `data-left`) |
| `.check-table` | balance check | tick "matches" or type the real balance per account |
| `details.why` | dashboard | "How these figures are worked out" with the user's own numbers |
| `.glossary` | help | term / definition grid |
| `.searchbar` | search | the search box and button |
| `.flash.ok` + `.undo` | every page | the Undo button inside the success message |
| `.drawer#calc` | every page | the calculator drawer (button or C) |

### Page blocks for the existing templates

The second half of `design.css` ("Page blocks") styles the app's older markup in v3: `.panel` sections,
`.stats > .stat` numbers, `.item` lists, `.pill` status text (a square + word), `.tabs` with `.active`,
`.list`, `.split`/`.split.sum`, `.checks`, `.audit`, `.table`, `<label>` + input forms in `.cols2`/`.cols3`,
`fieldset.back`/`.lay`, `.calc-out` and `.leg-card`. Use the v3 components above for anything new, and
move pages onto them when you touch them.

### Data-driven sizes without inline styles

CSP forbids `style="…"`. Put the value in a data attribute and let `app.js` apply it:

```html
<div class="blk b-lay" data-v="7911"></div>          <!-- flex-grow in pence -->
<div class="drain"><i data-left="0.42"></i></div>     <!-- fraction of time left -->
<div class="loanbar"><span class="line" data-of="10000/17152"></span>…</div>
```

```js
document.querySelectorAll('.blk[data-v]').forEach(e => e.style.flexGrow = e.dataset.v);
document.querySelectorAll('.drain i[data-left]').forEach(e => e.style.width = (e.dataset.left * 100) + '%');
document.querySelectorAll('[data-of]').forEach(e => { const [a, b] = e.dataset.of.split('/'); e.style.width = (a / b * 100) + '%'; });
```

Charts are SVG built server-side or in `app.js`; SVG attributes (`x`, `width`, `fill="var(--gain)"`
via `class`) are allowed.

## 7. Copy

- Sentence case everywhere. Short, plain, active ("Mark withdrawn", toast "Logged as pending withdrawals").
- Name money by what it is to the user: Profit banked, Still to come, Free to bet, Held as liability,
  Staked, Cash at bookmakers, Borrowed from <bank>.
- Times: `Sat 23:59`, `Wed 14 Oct`; countdowns `1d 12h`. Money: `£1,234.56`, signed `+£7.93` / `−£0.14`
  (true minus sign).
- Errors say what's wrong and how to fix it. No apologies, no exclamation marks.

## 8. Checklist for a new or changed page

- [ ] Uses `design.css` classes and tokens only; nothing from `app.css`.
- [ ] The first screen answers "how am I doing / what do I need to do" for that page.
- [ ] Back is blue, lay is pink, gains green, losses red; no other colours.
- [ ] No inline styles or scripts; any new JS lives in `app.js`.
- [ ] Works at 390px wide with no sideways scroll (tables scroll inside `.tw`).
- [ ] Looks right in light and dark.
- [ ] Every figure comes from the services and matches the ledger.
