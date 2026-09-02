from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from token_recap.buckets import TokenBuckets
from token_recap.harness import display_rates
from token_recap.windows import Window

_CSI = re.compile(r"\x1b\[[0-9;]*m")
MIN_BOX = 80
# Every column after the name, each preceded by one space.
_COUNT_COLS = (1 + 7) + (1 + 7) + (1 + 7) + (1 + 6)
_PRICE_COLS = (1 + 9) + (1 + 7) + (1 + 8) + (1 + 7)


@dataclass(frozen=True)
class Palette:
    on: bool

    def wrap(self, code: str, text: str) -> str:
        if not self.on:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, text: str) -> str:
        return self.wrap("1", text)

    def dim(self, text: str) -> str:
        return self.wrap("2", text)

    def cyan(self, text: str) -> str:
        return self.wrap("36", text)

    def yellow(self, text: str) -> str:
        return self.wrap("33", text)

    def green(self, text: str) -> str:
        return self.wrap("32", text)

    def magenta(self, text: str) -> str:
        return self.wrap("35", text)

    def red(self, text: str) -> str:
        return self.wrap("31", text)

    def provider(self, name: str, text: str) -> str:
        code = {
            "claude": "38;5;208",
            "grok": "36",
            "codex": "32",
            "api": "35",
            "free": "2",
            "ollama": "34",
            "hyper": "38;5;213",
        }.get(name, "1")
        return self.wrap(code, text)


def color_enabled(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    if os.environ.get("NO_COLOR"):
        return False
    return True


def vis_len(text: str) -> int:
    return len(_CSI.sub("", text))


def fmt_int(n: int) -> str:
    return f"{n:,}"


def compact_count(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 100_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:,}"


def fmt_money(amount: float) -> str:
    if amount < 0.01 and amount > 0:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


def fmt_span(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "overdue"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def bar(frac: float, width: int, fill: str = "█", empty: str = "░") -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    filled = min(width, max(0, filled))
    return fill * filled + empty * (width - filled)


def _pad(text: str, width: int) -> str:
    extra = vis_len(text) - width
    if extra > 0:
        plain = _CSI.sub("", text)
        text = plain[: max(0, width - 1)] + "…"
    pad = width - vis_len(text)
    if pad <= 0:
        return text
    return text + " " * pad


def _box(
    title: str, body: list[str], pal: Palette, width: int, accent: str | None = None
) -> str:
    inner = width - 2
    head = pal.provider(accent, title) if accent else pal.bold(title)
    label = f" {head} "
    fill = inner - vis_len(label) - 1
    if fill < 1:
        fill = 1
    top = "┌─" + label + "─" * fill + "┐"
    lines = [top]
    for row in body:
        lines.append("│" + _pad(row, inner) + "│")
    lines.append("└" + "─" * inner + "┘")
    return "\n".join(lines)


def _window_frac(start: datetime, end: datetime, now: datetime) -> float:
    total = (end - start).total_seconds()
    if total <= 0:
        return 1.0
    return (now - start).total_seconds() / total


def _window_caption(start: datetime, end: datetime, now: datetime) -> str:
    length = fmt_span(end - start)
    if now < start:
        return f"starts in {fmt_span(start - now)}"
    if now < end:
        return f"{fmt_span(now - start)} in · {fmt_span(end - now)} left"
    if (now - end).total_seconds() < 60:
        return f"{length} to now"  # ends at this instant: running, not closed
    return f"closed · {length}"


def _mix_row(label: str, n: int, total: int, pal: Palette, tone: str) -> str:
    frac = (n / total) if total else 0.0
    colored = {
        "read": pal.cyan,
        "write": pal.yellow,
        "fresh": pal.red,
        "out": pal.magenta,
    }[tone]
    meter = colored(bar(frac, 22))
    pct = f"{frac * 100:5.1f}%"
    return f"  {label:<10} {meter}  {pct}  {pal.dim(compact_count(n))}"


def _model_row(
    name: str,
    width: int,
    calls: str,
    inp: str,
    cached: str,
    out: str,
    prices: tuple[str, str, str, str] | None,
) -> str:
    """Counts, then what they cost, then the unit price behind that cost.

    Model names are never abbreviated: the box grows to fit the longest one.
    An id says which of two near-identical models and providers a row is, so
    an ellipsis through the middle of it destroys the row's whole point.
    """
    row = f"  {_pad(name, width)} {calls:>7} {inp:>7} {cached:>7} {out:>6}"
    if prices is None:
        return row
    total, rate_in, rate_cached, rate_out = prices
    return f"{row} {total:>9} {rate_in:>7} {rate_cached:>8} {rate_out:>7}"


def fmt_rate(value: float) -> str:
    """A $/MTok figure: cents matter at the top, fractions of one at the tail."""
    if value < 0.01:
        return f"${value:.4f}" if value > 0 else "$0"
    return f"${value:.2f}"


def model_names(buckets: TokenBuckets) -> list[str]:
    """The names as rendered, for sizing the column that holds them."""
    return [_short_model(name) for name in buckets.models]


def _short_model(name: str) -> str:
    return name.replace("claude-", "").replace("-20251001", "")


def models_lines(
    buckets: TokenBuckets, pal: Palette, width: int, prices: bool = True
) -> list[str]:
    """A row per model: a joined list loses its tail to the box edge.

    Every column is labelled, because an unlabelled tally sitting under a
    dollar figure reads like a breakdown of that figure.
    """
    rows: list[str] = []
    ranked = buckets.by_cost() if prices else buckets.by_output()
    for model, use in ranked:
        short = _short_model(model)
        if short == "<synthetic>":
            continue  # no request was made, so there is nothing to attribute
        # The unit price sits beside the count it explains, so a row can be
        # read as money without going and looking the card up. A dash means no
        # published rate, which is not the same as a rate of zero.
        priced: tuple[str, str, str, str] | None = None
        if prices:
            rates = display_rates(use.model_id or model, use.provider)
            priced = (
                fmt_money(use.native_usd),
                fmt_rate(rates.input) if rates else "-",
                fmt_rate(rates.cache_read) if rates else "-",
                fmt_rate(rates.output) if rates else "-",
            )
        rows.append(
            pal.dim(
                _model_row(
                    short,
                    width,
                    fmt_int(use.calls),
                    compact_count(use.uncached),
                    compact_count(use.cached),
                    compact_count(use.output),
                    priced,
                )
            )
        )
    if not rows:
        return [pal.dim("  no model breakdown")]
    header = _model_row(
        "model",
        width,
        "calls",
        "in",
        "cached",
        "out",
        ("total", "$in/M", "$cache/M", "$out/M") if prices else None,
    )
    return [pal.dim(header)] + rows


@dataclass(frozen=True)
class Meter:
    """A provider's own reading of the quota it is metering you on.

    Not a token count and not comparable to one: providers weight it by model
    and count usage this machine's logs never see.
    """

    used: float | None  # 0..1, or None when the provider will not say
    note: str


@dataclass
class ProviderView:
    key: str
    title: str
    window: Window
    buckets: TokenBuckets
    meter: Meter | None = None
    source: str = ""  # who reported this section, when the rows no longer say
    prices: bool = True
    short: str = ""  # the summary-table name; the title is often too long

    @property
    def summary_name(self) -> str:
        return self.short or self.key.capitalize()


def render_provider(
    view: ProviderView, now: datetime, pal: Palette, width: int, name_col: int
) -> str:
    b = view.buckets
    start, end = view.window.start, view.window.end
    when = f"{start:%d %b %H:%M} → {end:%d %b %H:%M}"
    body = [
        f"  {pal.dim(view.window.label)}  ·  {when}",
        # Both bars are drawn and both are labelled: they look identical but
        # mean different things, and reading one for the other is the
        # difference between "82% spent" and "82% of the week has passed".
        f"  {pal.dim('elapsed')} {pal.cyan(bar(_window_frac(start, end, now), 20))}"
        f"  {pal.dim(_window_caption(start, end, now))}",
    ]
    if view.meter is not None:
        body.append(f"  {pal.dim('used   ')} {_meter_bar(view.meter, pal)}")
    if view.source:
        body.append(f"  {pal.dim(view.source)}")
    body += [
        "",
        f"  {pal.bold(compact_count(b.total) + ' tokens')}   {pal.dim(fmt_int(b.calls) + ' calls')}",
        _mix_row("cache hit", b.cache_read, b.total, pal, "read"),
        _mix_row("write", b.cache_write, b.total, pal, "write"),
        _mix_row("uncached", b.uncached, b.total, pal, "fresh"),
        _mix_row("output", b.output, b.total, pal, "out"),
        "",
    ]
    if view.prices:
        body.append(
            f"  {pal.dim(_pad('API list', 16))}"
            f" {pal.yellow(pal.bold(f'{fmt_money(b.native_usd):>10}'))}"
        )
    body += models_lines(b, pal, name_col, view.prices)
    return _box(view.title, body, pal, width, accent=view.key)


def _meter_bar(meter: Meter, pal: Palette) -> str:
    """The provider's own reading, or an empty gauge saying it gave none."""
    if meter.used is None:
        return f"{pal.dim(bar(0.0, 20))}  {pal.dim(meter.note)}"
    caption = f"{meter.used * 100:.0f}%"
    if meter.note:
        caption += f" · {meter.note}"
    return f"{pal.cyan(bar(meter.used, 20))}  {pal.dim(caption)}"


def render_header(
    now: datetime, custom: bool, weeks: int, pal: Palette, width: int
) -> str:
    stamp = now.strftime("%a %d %b %Y  %H:%M %Z")
    plain = not custom and weeks == 1
    if custom:
        mode = "Custom range on every provider."
    elif weeks > 1:
        mode = f"The last {weeks} weeks, to each provider's own reset."
    else:
        mode = "Each provider's own weekly window."
    source = "'logged' is read from the provider; 'local' is an observed clock."
    priced = "API list $ = what these tokens would cost at the provider's"
    caveat = "own list rate, per model. Not what you actually paid."
    body = [f"  {pal.bold(stamp)}", f"  {pal.dim(mode)}"]
    if plain:
        body.append(f"  {pal.dim(source)}")
    body.append(f"  {pal.dim(priced)}")
    body.append(f"  {pal.dim(caveat)}")
    return _box("Token recap", body, pal, width)


def render_summary(
    views: list[ProviderView],
    combined: TokenBuckets,
    pal: Palette,
    width: int,
) -> str:
    header = f"  {'':<8}{'tokens':>8}  {'API list':>12}"
    rule = pal.dim("  " + "─" * 30)
    rows = [pal.dim(header), rule]
    for view in views:
        rows.append(
            f"  {view.summary_name:<8}{compact_count(view.buckets.total):>8}  "
            f"{pal.yellow(f'{fmt_money(view.buckets.native_usd):>12}')}"
        )
    rows.append(rule)
    rows.append(
        f"  {pal.bold(_pad('total', 8))}"
        f"{pal.bold(f'{compact_count(combined.total):>8}')}  "
        f"{pal.yellow(pal.bold(f'{fmt_money(combined.native_usd):>12}'))}"
    )
    rows.append("")
    rows.append(pal.dim("  Local logs, not the subscription meter. Compare token"))
    rows.append(pal.dim("  totals (or API list $) with the % used in each web UI."))
    return _box("Snapshot", rows, pal, width)


def render_report(
    now: datetime,
    custom: bool,
    weeks: int,
    views: list[ProviderView],
    combined: TokenBuckets,
    color: bool | None = None,
) -> str:
    pal = Palette(color_enabled(color))
    # Each table sizes its own name column to its own longest model, so a
    # section of short names is not stretched by some other section's long
    # ones. The boxes still share one width — trailing space inside a narrow
    # table costs nothing, whereas ragged boxes are hard to read down.
    name_cols = {id(view): _name_col(view) for view in views}
    width = max(
        [MIN_BOX] + [2 + name_cols[id(view)] + _cols(view) + 2 for view in views]
    )
    chunks = [render_header(now, custom, weeks, pal, width)]
    chunks.extend(
        render_provider(view, now, pal, width, name_cols[id(view)]) for view in views
    )
    chunks.append(render_summary(views, combined, pal, width))
    return "\n".join(chunks) + "\n"


def _name_col(view: ProviderView) -> int:
    return max([len("model")] + [len(name) for name in model_names(view.buckets)])


def _cols(view: ProviderView) -> int:
    return _COUNT_COLS + (_PRICE_COLS if view.prices else 0)
