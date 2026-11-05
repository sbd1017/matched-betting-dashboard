# Verified market outcomes

`bets/exposure.py` stores an explicit, exhaustive outcome set per position, including each leg’s net P&L in integer pence. It does not infer correlation from event names. For each exchange, open lay exposure is the largest aggregate loss across those outcomes; separate positions and exchanges remain separate. Screen-quoted penny amounts can be retained despite rounded displayed odds.

Ordinary positions retain the existing calculation. A changed leg, account, stake, odds, commission or token invalidates the saved outcome set: exposure returns to the conservative sum, projections are withheld and an issue requests review. Unrelated bets, ledger entries and profit are untouched. Configured positions must be settled by leg, since a back win does not imply that all lays lose.

An administrator can configure verified outcomes using `exposure.configure` in a database transaction; this is audited. New complex positions need an explicit reviewed outcome set. No automatic score-market inference or editing UI is added.

## Lays in one exchange market

A position's outcomes cover its own lays. An exchange also nets across positions: lays in one market can't all lose, so it holds that market's worst outcome. `exposure.market_holds` does the same for lays given a market with `exposure.set_market` (the `set_lay_market` change, or Exchange market on the position page). It feeds what an exchange holds and available cash; each position's and bet's own liability stays as it was.

The market is what the user names, never worked out from event names, and names are compared through `bets/normalise.py`. Different markets and different exchanges never net. A lay with no market counts in full, so nothing nets until the user says it should; a market named wrongly does make available cash read too high, so name it from the exchange screen.
