"""HTML rendering of the usage dashboard (Telegram parse_mode=HTML, every message < 4096 chars)."""

from __future__ import annotations

import time
from datetime import datetime

from src.services.quota import EXTRA_WINDOWS, QuotaResult, QuotaWindow
from src.services.usage_tracker import (
    TTL_1H,
    DayStats,
    SessionView,
    Tokens,
    TopItem,
    UsageSummary,
)
from src.utils.text import TG_SAFE_LIMIT, esc, esc_fit, truncate

BAR_WIDTH = 20
DAY_BAR_WIDTH = 10
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def fmt_tokens(n: int | float) -> str:
    """CLI style: 580, 315.3k, 40.2m."""
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:.0f}"


def fmt_usd(value: float) -> str:
    return f"${value:,.2f}".replace(",", " ")


def fmt_span(seconds: float) -> str:
    """1д 1ч 35м · 4ч 45м · 55м 20с · 12с."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}д {hours}ч {minutes}м"
    if hours:
        return f"{hours}ч {minutes}м"
    if minutes:
        return f"{minutes}м {secs}с"
    return f"{secs}с"


def progress_bar(percent: float, width: int = BAR_WIDTH) -> str:
    """`████▌░░░` with half-cell precision, clamped to 0-100%."""
    cells = max(0.0, min(100.0, percent)) / 100 * width
    full = int(cells)
    half = cells - full >= 0.5 and full < width
    return "█" * full + ("▌" if half else "") + "░" * (width - full - (1 if half else 0))


def severity(percent: float) -> str:
    return "🔴" if percent >= 90 else "🟠" if percent >= 70 else "🟢"


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def _utc_offset() -> str:
    offset = datetime.now().astimezone().utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")


def reset_text(resets_at: float | None, now: float) -> str:
    if resets_at is None:
        return "время сброса неизвестно"
    when = _local(resets_at)
    left = resets_at - now
    stamp = when.strftime("%H:%M") if when.date() == _local(now).date() else when.strftime("%d.%m в %H:%M")
    prefix = "Сброс в " if when.date() == _local(now).date() else "Сброс "
    tail = f"через {fmt_span(left)}" if left > 0 else "уже должен был пройти, обновите"
    return f"{prefix}{stamp} ({tail})"


def _window_block(title: str, window: QuotaWindow | None, now: float) -> list[str]:
    if window is None:
        return [f"<b>{title}</b>", "<i>нет данных от API</i>"]
    pct = window.utilization
    return [
        f"<b>{title}</b>",
        f"<code>{progress_bar(pct)}</code> <b>{pct:.0f}%</b> {severity(pct)}",
        f"🔄 {reset_text(window.resets_at, now)}",
    ]


def render_quota(result: QuotaResult | None, now: float | None = None, enabled: bool = True) -> str:
    now = time.time() if now is None else now
    if not enabled:
        return "<i>📊 Живые квоты выключены (USAGE_QUOTA_ENABLED=false)</i>"
    snap = result.snapshot if result else None
    if snap is None:
        reason = result.error if result and result.error else "нет данных"
        return f"<b>📊 Квоты подписки</b>\n⚠️ Не удалось получить: {esc(reason)}"
    lines = _window_block("⏳ Текущая сессия (окно 5 ч)", snap.session, now)
    lines.append("")
    lines += _window_block("📅 Неделя (все модели)", snap.week, now)
    for key, title in EXTRA_WINDOWS.items():
        if key in snap.extra:
            w = snap.extra[key]
            lines.append(f"{title}: <code>{progress_bar(w.utilization, 10)}</code> {w.utilization:.0f}%")
    stamp = _local(snap.fetched_at).strftime("%H:%M:%S")
    plan = f" · подписка {esc(snap.subscription)}" if snap.subscription else ""
    if result is not None and result.live:
        lines.append(f"<i>🟢 Живые данные API · {stamp}{plan}</i>")
    else:
        age = fmt_span(now - snap.fetched_at)
        reason = esc_fit(result.error, 200) if result and result.error else "нет связи"
        lines.append(f"<i>⚠️ Живой запрос не удался ({reason}). Снимок от {stamp} ({age} назад)</i>")
    return "\n".join(lines)


def render_tokens(t: Tokens) -> str:
    return (f"{fmt_tokens(t.input)} вх · {fmt_tokens(t.output)} вых · {fmt_tokens(t.cache_read)} чтение · "
            f"{fmt_tokens(t.cache_write)} запись")


def render_session(view: SessionView | None, now: float | None = None, selected: bool = True) -> str:
    now = time.time() if now is None else now
    if view is None:
        return "<b>💬 Сессия</b>\n<i>Нет данных: выберите чат в 📂 Сессии</i>"
    title = f" «{esc_fit(view.title, 60)}»" if view.title else ""
    origin = "выбранный чат" if selected else "последний активный чат"
    lines = [f"<b>💬 Сессия</b>{title} <code>{esc(view.session_id[:8])}</code> <i>({origin})</i>"]
    parts = [f"💵 <b>{fmt_usd(view.cost)}</b>"]
    if view.api_ms is not None:
        parts.append(f"⚙️ API {fmt_span(view.api_ms / 1000)}")
    if view.wall_ms is not None:
        parts.append(f"🕰 {fmt_span(view.wall_ms / 1000)}")
    lines.append(" · ".join(parts))
    lines.append(f"✏️ +{view.lines_added} / -{view.lines_removed} строк кода")
    for model in view.models[:4]:
        lines.append(f"🤖 {esc(model.model)}: {render_tokens(model.tokens)} ({fmt_usd(model.cost)})")
    cache = view.cache
    misses = len(cache.misses)
    lines.append(
        f"🗄 Кэш промптов: {cache.requests} запр. · {cache.hit_share:.0%} входа из кэша · "
        f"{misses} {'промах' if misses == 1 else 'промахов' if misses == 0 or misses >= 5 else 'промаха'}"
    )
    if cache.misses:
        last = cache.misses[-1]
        lines.append(
            f"   последний {fmt_span(now - last.ts)} назад: {esc(last.cause)}, "
            f"перезаписано {fmt_tokens(last.recached_tokens)} токенов"
        )
    ttl = "1h" if cache.ttl_sec == TTL_1H else "5m"
    if cache.idle_sec is None:
        lines.append("🧊 Кэш: запросов еще не было")
    elif cache.warm:
        lines.append(
            f"🔥 Кэш тёплый (осталось {fmt_span(cache.ttl_left_sec)} до сброса {ttl} TTL): продолжать чат сейчас выгодно"
        )
    else:
        lines.append(
            f"❄️ Кэш остыл (простой {fmt_span(cache.idle_sec)}): следующий запрос перезапишет "
            f"~{fmt_tokens(cache.rewrite_tokens)} токенов в кэш"
        )
    lines.append(f"📏 Текущий контекст чата: {fmt_tokens(cache.context_tokens)} токенов")
    if view.compacts:
        last_compact = view.compacts[-1]
        lines.append(
            f"🧹 Последнее сжатие {fmt_span(now - last_compact.ts)} назад: "
            f"{fmt_tokens(last_compact.pre_tokens)} → {fmt_tokens(last_compact.post_tokens)}"
        )
    return "\n".join(lines)


def render_costs(s: UsageSummary) -> str:
    week_avg = s.week.cost / 7
    lines = [
        "<b>💵 Расходы</b> <i>(по ценам API, часовой пояс ПК)</i>",
        f"Сегодня: <b>{fmt_usd(s.today.cost)}</b> · {s.today.requests} запр. · {s.today.sessions} сесс.",
        f"Вчера: {fmt_usd(s.yesterday.cost)} · {s.yesterday.requests} запр.",
        f"7 дней: <b>{fmt_usd(s.week.cost)}</b> (в среднем {fmt_usd(week_avg)}/день)",
        f"30 дней: {fmt_usd(s.month.cost)} · Всё время: {fmt_usd(s.all_time.cost)}",
    ]
    if s.unpriced_models:
        lines.append(f"<i>Нет цен для: {esc_fit(', '.join(s.unpriced_models), 200)} (не учтены)</i>")
    return "\n".join(lines)


def render_diagnostics(s: UsageSummary) -> str:
    d = s.diagnostics
    lines = ["<b>🔍 Что расходует лимиты (24 ч)</b>"]
    if not d.requests:
        lines.append("<i>За сутки запросов не было</i>")
        return "\n".join(lines)
    icon = "⚠️" if d.heavy_share >= 0.5 else "ℹ️"
    lines.append(
        f"{icon} {d.heavy_share:.0%} запросов с контекстом &gt;{fmt_tokens(d.threshold)} токенов "
        f"({d.heavy_requests} из {d.requests}, {d.heavy_cost_share:.0%} стоимости)"
    )
    if d.model_shares:
        lines.append("🤖 Модели: " + " · ".join(f"{esc(name)} {share:.0%}" for name, share in d.model_shares[:4]))
    if d.heavy_share >= 0.5:
        lines.append("💡 Длинный контекст дорог: 🧹 сожмите чат или начните 🆕 чистую сессию")
    return "\n".join(lines)


def render_dashboard(
    quota: QuotaResult | None, summary: UsageSummary, view: SessionView | None, selected: bool,
    quota_enabled: bool = True, now: float | None = None,
) -> str:
    now = time.time() if now is None else now
    stamp = _local(now).strftime("%d.%m %H:%M")
    blocks = [
        f"📊 <b>Расход и лимиты Claude</b> · {stamp} ({_utc_offset()})",
        render_quota(quota, now, quota_enabled),
        render_session(view, now, selected),
        render_costs(summary),
        render_diagnostics(summary),
    ]
    return fit_message("\n\n".join(blocks))


def fit_message(text: str, limit: int = TG_SAFE_LIMIT) -> str:
    """Trim whole lines from the end so HTML tags are never cut in the middle."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    while lines and len("\n".join(lines)) > limit - 2:
        lines.pop()
    return "\n".join(lines) + "\n…"


def day_label(day: DayStats) -> str:
    return f"{WEEKDAYS[day.day.weekday()]} {day.day.strftime('%d.%m')}"


def render_days(summary: UsageSummary, n: int) -> str:
    days = summary.last_days(n)
    peak = max((d.cost for d in days), default=0.0)
    rows = []
    for d in reversed(days):  # today first
        filled = round(d.cost / peak * DAY_BAR_WIDTH) if peak > 0 else 0
        if d.cost > 0 and filled == 0:
            filled = 1
        bar = "▰" * filled + "▱" * (DAY_BAR_WIDTH - filled)
        rows.append(f"{day_label(d)} {bar} {fmt_usd(d.cost):>9} {d.requests:>4} запр.")
    total = sum(d.cost for d in days)
    tokens = Tokens()
    for d in days:
        tokens.add(d.tokens)
    active = [d for d in days if d.requests]
    lines = [f"📅 <b>Расходы по дням: {n} дн.</b>", "<pre>" + esc("\n".join(rows)) + "</pre>"]
    lines.append(f"Итого: <b>{fmt_usd(total)}</b> · в среднем {fmt_usd(total / n)}/день · активных дней: {len(active)}")
    if active:
        top = max(active, key=lambda d: d.cost)
        lines.append(f"Пик: {day_label(top)} · {fmt_usd(top.cost)} · {top.requests} запр.")
    lines.append(f"Токены: {fmt_tokens(tokens.total)} ({render_tokens(tokens)})")
    return fit_message("\n".join(lines))


def _top_lines(title: str, items: list[TopItem], code: bool) -> list[str]:
    lines = [f"<b>{title}</b>"]
    if not items:
        lines.append("<i>нет запусков</i>")
    for i, item in enumerate(items, 1):
        label = esc(truncate(item.label, 60))
        label = f"<code>{label}</code>" if code else f"«{label}»"
        lines.append(f"{i}. {label}: <b>{fmt_usd(item.cost)}</b> · {item.requests} запр.")
    return lines


def render_top(projects_7: list[TopItem], chats_7: list[TopItem], projects_30: list[TopItem],
               chats_30: list[TopItem]) -> str:
    lines = ["📁 <b>Самые дорогие проекты и чаты</b>", ""]
    lines += _top_lines("📁 Проекты · 7 дней", projects_7, code=True)
    lines.append("")
    lines += _top_lines("💬 Чаты · 7 дней", chats_7, code=False)
    lines.append("")
    lines += _top_lines("📁 Проекты · 30 дней", projects_30, code=True)
    lines.append("")
    lines += _top_lines("💬 Чаты · 30 дней", chats_30, code=False)
    return fit_message("\n".join(lines))


def status_line(quota: QuotaResult | None, summary: UsageSummary) -> str:
    """Compact line for /status: `📊 Квоты: 5ч 5% · Нед 97% | 💵 Сегодня: $4.13 (7д: $276.12)`."""
    snap = quota.snapshot if quota else None
    if snap is not None:
        five = f"{snap.session.utilization:.0f}%" if snap.session else "?"
        week = f"{snap.week.utilization:.0f}%" if snap.week else "?"
        stale = "" if quota and quota.live else " (снимок)"
        quotas = f"📊 Квоты: 5ч {five} · Нед {week}{stale}"
    else:
        quotas = "📊 Квоты: н/д"
    return f"{quotas} | 💵 Сегодня: {fmt_usd(summary.today.cost)} (7д: {fmt_usd(summary.week.cost)})"
