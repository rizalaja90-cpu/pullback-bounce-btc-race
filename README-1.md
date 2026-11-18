# PullbackBounce — a dip-buy bot for the make.fun cbBTC 24h race

A bot that trades Coinbase Wrapped BTC (cbBTC) against USDC, one minute at a
time, in the `agent-fun/bars-replay/v1` replay. It starts with 1,000 USDC of
paper money and races for the highest net PnL over 24 hours.

## How it trades

- **Entry:** the close has pulled back at least 0.80% from the highest close
  of the last 60 bars, and the last two closes are both up — the bounce has
  started, not just a single green bar. It buys with all cash.
- **Exit:** take profit at +1.00% over the entry fill, stop out at -0.50%,
  or after 90 bars held, whichever comes first. It sells the whole position.
- **Guards:** 150 bars of cooldown after a completed round trip, at most
  4 round trips per window, and no new entries in the last 30 bars.

## Run

```sh
python3 -P -s main.py
```

Standard library only. No network, no wall clock, no files outside the
repository, no randomness — two runs on the same data answer byte for byte
the same. All decisions use integer basis-point math on the prices as sent.

## Dev-window result (local replay, official fill model)

Net PnL **+$9.80**, 1 trade, max drawdown 0.12% — buy $63,617.14, sell
$64,291.88. Deterministic across repeated runs.
