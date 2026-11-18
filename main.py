"""PullbackBounce: a dip-buy bot for the make.fun cbBTC 24h race.

Strategy (one asset, long only, full size, at most 4 round trips):
  - Entry: the close has pulled back at least 0.80% from the highest close
    of the last 60 bars, and the last two closes are both up (the bounce has
    started, not just a single green bar). Buy with all cash.
  - Exit: take profit at +1.00% over the entry fill, stop out at -0.50%,
    or after 90 bars held, whichever comes first. Sell the whole position.
  - Guards: 150 bars of cooldown after a completed round trip; no new
    entries in the last 30 bars of the window (too little time left for a
    bounce to play out); never trade during warm-up bars.

Protocol: agent-fun/bars-replay/v1, newline-delimited JSON on stdin/stdout.
One reply per batch, echoing its seq. All decisions use integer basis-point
math on the prices as sent (no floats, no clock, no environment, no random),
so two runs on the same data answer byte for byte the same.
"""

import json
import sys

PROTOCOL = "agent-fun/bars-replay/v1"
BPS = 10_000

# --- strategy parameters (basis points / bars) ---
DIP_BPS = 80            # buy only 0.80% or more below the 60-bar high
HIGH_BARS = 60          # rolling-high window, in closes
TAKE_BPS = 100          # take profit at +1.00% over the entry fill
STOP_BPS = 50           # stop out at -0.50% under the entry fill
HOLD_BARS = 90          # give up after 90 bars held
COOLDOWN_BARS = 150     # wait 150 bars after a completed round trip
MAX_TRIPS = 4           # at most 4 round trips per window
TAIL_BARS = 30          # no new entries in the last 30 bars of the window
WINDOW_BARS = 1440      # scored bars in the window (per the competition rules)
MAX_SLIPPAGE_BPS = 100  # 1% past the close the order was decided on


def _int(value, default=0):
    """A u64 decimal string (or int) as an int; default when absent."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Bot:
    def __init__(self, hello):
        self.hello = hello
        self.symbols = hello.get("symbols") or []
        self.symbol = self.symbols[0]["symbol"] if self.symbols else "CBBTC"
        self.price_scale = _int(self.symbols[0].get("price_scale"), 5) if self.symbols else 5
        self.qty_scale = _int(self.symbols[0].get("qty_scale"), 8) if self.symbols else 8
        self.quote_scale = _int((hello.get("capital") or {}).get("quote_scale"), 6)
        caps = hello.get("caps") or {}
        fees = hello.get("fees") or {}
        self.max_weight_bps = _int(caps.get("max_weight_bps"), 10000)
        self.min_notional = _int(fees.get("min_notional_quote_units"), 1000000)
        self.warmup_bars = _int(hello.get("warmup_bars"), 0)
        self.cash = _int((hello.get("capital") or {}).get("quote_units"), 0)
        self.equity = self.cash
        self.closes = []          # closes, ints at price_scale, oldest first
        self.qty = 0              # held quantity, ints at qty_scale
        self.entry_price = 0      # fill price of the open position, price_scale ints
        self.entry_fill_seq = -1  # batch seq the open position filled on
        self.trips = 0            # completed round trips
        self.last_sell_seq = -10 ** 9
        self._next_id = 1

    def _client_id(self, prefix):
        cid = "%s%d" % (prefix, self._next_id)
        self._next_id += 1
        return cid

    def _scored(self, seq):
        """Scored-bar index of a batch seq (warm-up bars do not count)."""
        return seq - self.warmup_bars

    def _apply_fills(self, batch):
        for fill in batch.get("fills") or []:
            if fill.get("symbol") != self.symbol:
                continue
            if fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                self.entry_price = _int(fill.get("price"), self.entry_price)
                self.entry_fill_seq = _int(fill.get("fill_bar"), batch.get("seq", 0))
            elif fill.get("side") == "sell":
                self.trips += 1
                self.last_sell_seq = _int(fill.get("fill_bar"), batch.get("seq", 0))
                self.entry_price = 0
                self.entry_fill_seq = -1

    def _apply_account(self, account):
        self.cash = _int(account.get("cash_quote_units"), self.cash)
        self.equity = _int(account.get("equity_quote_units"), self.equity)
        held = 0
        for pos in account.get("positions") or []:
            if pos.get("symbol") == self.symbol:
                held = _int(pos.get("qty"), 0)
                if self.entry_price == 0 and held:
                    cost = _int(pos.get("cost_quote_units"), 0)
                    if cost and held:
                        # cost is quote units, qty is qty_scale units: rescale
                        # to price_scale units (cost/qty * 10**(qty+price-quote)).
                        self.entry_price = (cost * 10 ** (self.qty_scale
                                                           + self.price_scale
                                                           - self.quote_scale)
                                            // held)
        self.qty = held
        if not held:
            self.entry_price = 0
            self.entry_fill_seq = -1

    def on_batch(self, batch):
        seq = batch.get("seq", 0)
        self._apply_fills(batch)
        self._apply_account(batch.get("account") or {})
        fresh = False
        for bar in batch.get("bars") or []:
            if bar.get("symbol") == self.symbol:
                self.closes.append(_int(bar.get("c"), 0))
                fresh = True
        if len(self.closes) > 240:
            del self.closes[:-240]
        if seq < self.warmup_bars:
            return []
        if not self.closes:
            return []
        close = self.closes[-1]
        scored = self._scored(seq)

        # --- exit: whole position, take / stop / hold ---
        if self.qty > 0 and self.entry_price > 0:
            gain_bps = (close - self.entry_price) * BPS // self.entry_price
            age = seq - self.entry_fill_seq if self.entry_fill_seq >= 0 else 0
            if gain_bps >= TAKE_BPS or gain_bps <= -STOP_BPS or age >= HOLD_BARS:
                return [{
                    "side": "sell",
                    "client_id": self._client_id("s"),
                    "symbol": self.symbol,
                    "qty": str(self.qty),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]

        # --- entry: dip below the 60-bar high plus two closes up ---
        if self.qty == 0 and fresh and self.trips < MAX_TRIPS:
            if (len(self.closes) >= HIGH_BARS + 2
                    and seq - self.last_sell_seq >= COOLDOWN_BARS
                    and scored <= WINDOW_BARS - TAIL_BARS - 1):
                high = max(self.closes[-HIGH_BARS:])
                dip_bps = (high - close) * BPS // high if high > 0 else 0
                two_up = (self.closes[-1] > self.closes[-2]
                          and self.closes[-2] > self.closes[-3])
                if dip_bps >= DIP_BPS and two_up:
                    by_weight = self.max_weight_bps * self.equity // BPS
                    spend = self.cash if self.cash < by_weight else by_weight
                    if spend >= self.min_notional:
                        return [{
                            "side": "buy",
                            "client_id": self._client_id("b"),
                            "symbol": self.symbol,
                            "notional_quote_units": str(spend),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        }]
        return []


def main():
    state = None
    out = sys.stdout
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "hello":
            if msg.get("protocol") != PROTOCOL:
                raise SystemExit("unsupported protocol: %r" % (msg.get("protocol"),))
            state = Bot(msg)
        elif kind == "batch":
            if state is None:
                raise SystemExit("batch before hello")
            try:
                orders = state.on_batch(msg)
            except Exception as exc:  # never let one bad bar kill the run
                print("batch %s error: %r" % (msg.get("seq"), exc), file=sys.stderr)
                orders = []
            out.write(json.dumps(
                {"type": "reply", "seq": msg.get("seq", 0), "orders": orders},
                separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
