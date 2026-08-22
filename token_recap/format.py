from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from token_recap.buckets import Rates, TokenBuckets
from token_recap.windows import WeeklyReset

_CSI = re.compile(r"\x1b\[[0-9;]*m")
BOX_WIDTH = 64


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
        code = {"claude": "38;5;208", "grok": "36", "codex": "32"}.get(name, "1")
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


def _box(title: str, body: list[str], pal: Palette, accent: str | None = None) -> str:
    inner = BOX_WIDTH - 2
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


def _models_line(buckets: TokenBuckets, pal: Palette) -> str:
    parts: list[str] = []
    for model, n in buckets.models.most_common():
        short = model.replace("claude-", "").replace("-20251001", "")
        if short == "<synthetic>":
            continue
        parts.append(f"{short} {fmt_int(n)}")
    if not parts:
        return pal.dim("  no model breakdown")
    return pal.dim("  " + " · ".join(parts))


def fmt_mult(mult: float) -> str:
    if abs(mult - round(mult)) < 1e-9:
        return str(int(round(mult)))
    return f"{mult:g}"


def _cheaper(native: float, cheap: float) -> str:
    if cheap <= 0 or native <= 0:
        return ""
    ratio = native / cheap
    if ratio < 1.05:
        return ""
    if ratio >= 10:
        return f"{ratio:.0f}× cheaper"
    return f"{ratio:.1f}× cheaper"


def _money_row(pal: Palette, label: str, amount: float, note: str = "") -> str:
    row = f"  {pal.dim(_pad(label, 16))} {pal.green(pal.bold(f'{fmt_money(amount):>10}'))}"
    if note:
        row += f"  {pal.dim(note)}"
    return row


@dataclass
class ProviderView:
    key: str
    title: str
    spec: WeeklyReset | None
    start: datetime
    end: datetime
    buckets: TokenBuckets
    native_label: str


def _bucket_cost(rates: Rates, buckets: TokenBuckets) -> float:
    return rates.cost(
        buckets.uncached, buckets.cache_write, buckets.cache_read, buckets.output
    )


def render_provider(
    view: ProviderView,
    now: datetime,
    flash: Rates,
    pro: Rates,
    token_mult: float,
    pal: Palette,
) -> str:
    b = view.buckets
    flash_1x = _bucket_cost(flash, b)
    flash_nx = flash_1x * token_mult
    pro_1x = _bucket_cost(pro, b)
    frac = _window_frac(view.start, view.end, now)
    week_bar = pal.cyan(bar(frac, 20))
    reset = view.spec.label + " local" if view.spec else "custom window"
    when = f"{view.start:%d %b %H:%M} → {view.end:%d %b %H:%M}"
    n = fmt_mult(token_mult)
    body = [
        f"  {pal.dim(reset)}  ·  {when}",
        f"  {week_bar}  {pal.dim(_window_caption(view.start, view.end, now))}",
        "",
        f"  {pal.bold(compact_count(b.total) + ' tokens')}   {pal.dim(fmt_int(b.calls) + ' calls')}",
        _mix_row("cache hit", b.cache_read, b.total, pal, "read"),
        _mix_row("write", b.cache_write, b.total, pal, "write"),
        _mix_row("uncached", b.uncached, b.total, pal, "fresh"),
        _mix_row("output", b.output, b.total, pal, "out"),
        "",
        f"  {pal.dim(_pad('API list', 16))} {pal.yellow(pal.bold(f'{fmt_money(b.native_usd):>10}'))}",
        _money_row(pal, "Flash  1×", flash_1x, _cheaper(b.native_usd, flash_1x)),
    ]
    if abs(token_mult - 1.0) > 1e-9:
        body.append(
            _money_row(
                pal,
                f"Flash  ×{n}",
                flash_nx,
                f"if it burns {n}× the tokens",
            )
        )
    body.extend(
        [
            _money_row(pal, "Pro    1×", pro_1x, "RunInfra"),
            _models_line(b, pal),
        ]
    )
    return _box(view.title, body, pal, accent=view.key)


def render_header(
    now: datetime,
    flash: Rates,
    pro: Rates,
    token_mult: float,
    custom: bool,
    pal: Palette,
) -> str:
    stamp = now.strftime("%a %d %b %Y  %H:%M %Z")
    mode = "Custom range on every provider." if custom else "Each provider has its own weekly reset."
    n = fmt_mult(token_mult)
    body = [
        f"  {pal.bold(stamp)}",
        f"  {pal.dim(mode)}",
        f"  {pal.dim(f'Flash RunInfra  ${flash.input}/${flash.cache_read}/${flash.output}')}",
        f"  {pal.dim(f'Pro   RunInfra  ${pro.input}/${pro.cache_read}/${pro.output}')}",
        f"  {pal.dim(f'Flash ×{n} scales every bucket (extra loops grow the cache too).')}",
    ]
    return _box("Token recap", body, pal)


def render_summary(
    views: list[ProviderView],
    combined: TokenBuckets,
    flash: Rates,
    pro: Rates,
    token_mult: float,
    pal: Palette,
) -> str:
    n = fmt_mult(token_mult)
    flash_1x = _bucket_cost(flash, combined)
    flash_nx = flash_1x * token_mult
    pro_1x = _bucket_cost(pro, combined)
    header = (
        f"  {'':<8}{'tokens':>7} {'API':>8} {'Flash':>8} "
        f"{('×' + n):>8} {'Pro':>8}"
    )
    rule = pal.dim("  " + "─" * 50)
    rows = [pal.dim(header), rule]
    for view in views:
        f1 = _bucket_cost(flash, view.buckets)
        p1 = _bucket_cost(pro, view.buckets)
        name = view.key.capitalize()
        rows.append(
            f"  {name:<8}{compact_count(view.buckets.total):>7} "
            f"{pal.yellow(f'{fmt_money(view.buckets.native_usd):>8}')} "
            f"{pal.green(f'{fmt_money(f1):>8}')} "
            f"{pal.green(f'{fmt_money(f1 * token_mult):>8}')} "
            f"{pal.green(f'{fmt_money(p1):>8}')}"
        )
    rows.append(rule)
    rows.append(
        f"  {pal.bold(_pad('total', 8))}{pal.bold(f'{compact_count(combined.total):>7}')} "
        f"{pal.yellow(pal.bold(f'{fmt_money(combined.native_usd):>8}'))} "
        f"{pal.green(pal.bold(f'{fmt_money(flash_1x):>8}'))} "
        f"{pal.green(pal.bold(f'{fmt_money(flash_nx):>8}'))} "
        f"{pal.green(pal.bold(f'{fmt_money(pro_1x):>8}'))}"
    )
    rows.append(
        pal.dim(f"  ×{n} = Flash if it spends {n}× tokens on the same task.")
    )
    rows.append("")
    rows.append(pal.dim("  Local logs, not the subscription meter. Compare token"))
    rows.append(pal.dim("  totals (or API list $) with the % used in each web UI."))
    return _box("Snapshot", rows, pal)


def render_report(
    now: datetime,
    flash: Rates,
    pro: Rates,
    token_mult: float,
    custom: bool,
    views: list[ProviderView],
    combined: TokenBuckets,
    color: bool | None = None,
) -> str:
    pal = Palette(color_enabled(color))
    chunks = [render_header(now, flash, pro, token_mult, custom, pal)]
    chunks.extend(
        render_provider(view, now, flash, pro, token_mult, pal) for view in views
    )
    chunks.append(
        render_summary(views, combined, flash, pro, token_mult, pal)
    )
    return "\n".join(chunks) + "\n"
