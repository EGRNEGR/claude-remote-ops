"""HTML rendering of the usage dashboard (Telegram parse_mode=HTML, every message < 4096 chars).

Main screen = the essentials (quota bars, today's spend, the chat's cost and cache); per-model tokens,
cache statistics and the "what burns the limits" diagnostics sit in expandable quotes.
"""

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
from src.ui import theme
from src.ui.formatter import fit_html
from src.utils.text import TG_SAFE_LIMIT, esc, esc_fit, plural, truncate

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


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def _utc_offset() -> str:
    offset = datetime.now().astimezone().utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")


def reset_text(resets_at: float | None, now: float) -> str:
    """`Сброс в 13:00 · через 3ч 20м` (plain text)."""
    if resets_at is None:
        return "Время сброса неизвестно"
    when = _local(resets_at)
    left = resets_at - now
    today = when.date() == _local(now).date()
    stamp = when.strftime("%H:%M") if today else when.strftime("%d.%m в %H:%M")
    tail = f"через {fmt_span(left)}" if left > 0 else "уже должен был пройти, обновите"
    return f"Сброс {'в ' if today else ''}{stamp} · {tail}"


def quota_value(window: QuotaWindow) -> str:
    """`▰▰▰▱▱▱▱▱▱▱▱▱ <b>35%</b> 🟢`."""
    pct = window.utilization
    return f"{theme.bar(pct)} <b>{pct:.0f}%</b> {theme.level_dot(pct)}"


def _window_block(label: str, window: QuotaWindow | None, now: float) -> str:
    if window is None:
        return theme.lines(theme.section(label), "<i>нет данных от API</i>")
    return theme.lines(theme.section(label), quota_value(window), theme.meta(reset_text(window.resets_at, now)))


def render_quota(result: QuotaResult | None, now: float | None = None, enabled: bool = True) -> str:
    now = time.time() if now is None else now
    if not enabled:
        return theme.meta("Живые квоты выключены (USAGE_QUOTA_ENABLED=false)")
    snap = result.snapshot if result else None
    if snap is None:
        reason = result.error if result and result.error else "нет данных"
        return theme.lines(theme.section("Квоты подписки"), f"{theme.DOT_ERROR} Не удалось получить: {esc(reason)}")
    blocks = [_window_block("Сессия · окно 5 ч", snap.session, now), _window_block("Неделя · все модели", snap.week, now)]
    extra = [
        theme.item(f"{esc(title)}: {theme.bar(snap.extra[key].utilization, 8)} {snap.extra[key].utilization:.0f}%")
        for key, title in EXTRA_WINDOWS.items() if key in snap.extra
    ]
    stamp = _local(snap.fetched_at).strftime("%H:%M:%S")
    plan = f"подписка {snap.subscription}" if snap.subscription else None
    if result is not None and result.live:
        source = theme.meta(f"{theme.DOT_OK} живые данные API", stamp, plan)
    else:
        reason = esc_fit(result.error, 200) if result and result.error else "нет связи"
        source = theme.meta_html(
            f"{theme.DOT_BUSY} живой запрос не удался ({reason})",
            f"Снимок от {stamp} ({fmt_span(now - snap.fetched_at)} назад)",
        )
    return theme.join_blocks(*blocks, theme.lines(*extra), source)


def quota_rows(result: QuotaResult | None, now: float | None = None) -> list[str]:
    """Compact rows for /status: `◈ Лимит 5 ч: ▰▰▱… <b>35%</b> 🟢 · сброс в 13:00 · через 3ч`."""
    now = time.time() if now is None else now
    snap = result.snapshot if result else None
    if snap is None:
        reason = result.error if result and result.error else "нет данных"
        return [f"{theme.MARK_SECTION} Лимиты: <b>недоступны</b> <i>· {esc_fit(reason, 200)}</i>"]
    rows = []
    for label, window in (("Лимит 5 ч", snap.session), ("Неделя", snap.week)):
        if window is None:
            continue
        reset = f" <i>· {esc(reset_text(window.resets_at, now).lower())}</i>" if window.resets_at else ""
        rows.append(f"{theme.MARK_SECTION} {label}: {quota_value(window)}{reset}")
    if result is not None and not result.live:
        rows.append(theme.meta(f"снимок от {_local(snap.fetched_at).strftime('%H:%M')}"))
    return rows


def spend_row(summary: UsageSummary | None) -> str:
    """`◈ Расход сегодня: <b>$4.13</b> · 7 дней $276.12` for /status."""
    if summary is None:
        return theme.field("Расход", "недоступен")
    return (f"{theme.MARK_SECTION} Расход сегодня: <b>{fmt_usd(summary.today.cost)}</b> "
            f"<i>· 7 дней {fmt_usd(summary.week.cost)}</i>")


def render_tokens(t: Tokens) -> str:
    return (f"{fmt_tokens(t.input)} вх · {fmt_tokens(t.output)} вых · {fmt_tokens(t.cache_read)} чтение · "
            f"{fmt_tokens(t.cache_write)} запись")


def _cache_state(view: SessionView) -> str:
    cache = view.cache
    ttl = "1h" if cache.ttl_sec == TTL_1H else "5m"
    if cache.idle_sec is None:
        return "🧊 Кэш: запросов еще не было"
    if cache.warm:
        return f"🔥 Кэш тёплый еще {fmt_span(cache.ttl_left_sec)} ({ttl} TTL): продолжать сейчас выгодно"
    return (f"❄️ Кэш остыл (простой {fmt_span(cache.idle_sec)}): следующий запрос перезапишет "
            f"~{fmt_tokens(cache.rewrite_tokens)} токенов")


def render_session(view: SessionView | None, now: float | None = None, selected: bool = True) -> str:
    now = time.time() if now is None else now
    if view is None:
        return theme.lines(theme.section("Чат"), theme.meta("Нет данных: выберите чат в 💬 Сессии"))
    origin = "выбранный чат" if selected else "последний активный чат"
    name = f" «{esc_fit(view.title, 60)}»" if view.title else ""
    head = f"{theme.section('Чат')}{name} <i>· {origin}</i>"
    cache = view.cache
    summary = f"<b>{fmt_usd(view.cost)}</b>{theme.SEP}контекст {fmt_tokens(cache.context_tokens)} токенов"
    misses = len(cache.misses)
    details = []
    timing = []
    if view.api_ms is not None:
        timing.append(f"API {fmt_span(view.api_ms / 1000)}")
    if view.wall_ms is not None:
        timing.append(f"всего {fmt_span(view.wall_ms / 1000)}")
    if timing:
        details.append(f"{theme.BULLET} Время: " + theme.SEP.join(timing))
    details.append(f"{theme.BULLET} Код: +{view.lines_added} / −{view.lines_removed} строк")
    for model in view.models[:4]:
        label = theme.model_name(model.model) or model.model
        details.append(f"{theme.BULLET} {esc(label)}: {render_tokens(model.tokens)} ({fmt_usd(model.cost)})")
    details.append(
        f"{theme.BULLET} Кэш промптов: {cache.requests} запр. · {cache.hit_share:.0%} входа из кэша · "
        f"{misses} {plural(misses, 'промах', 'промаха', 'промахов')}"
    )
    if cache.misses:
        last = cache.misses[-1]
        details.append(
            f"{theme.BULLET} Последний промах {fmt_span(now - last.ts)} назад: {esc(last.cause)}, "
            f"перезаписано {fmt_tokens(last.recached_tokens)} токенов"
        )
    if view.compacts:
        compact = view.compacts[-1]
        details.append(
            f"{theme.BULLET} Сжатие {fmt_span(now - compact.ts)} назад: "
            f"{fmt_tokens(compact.pre_tokens)} → {fmt_tokens(compact.post_tokens)}"
        )
    return theme.lines(head, summary, esc(_cache_state(view)), theme.quote("\n".join(details)))


def render_costs(s: UsageSummary) -> str:
    d = s.diagnostics
    head = f"{theme.section('Расход')} <i>· по ценам API, часовой пояс ПК</i>"
    main = theme.SEP.join([
        f"Сегодня <b>{fmt_usd(s.today.cost)}</b>",
        f"7 дней <b>{fmt_usd(s.week.cost)}</b>",
        f"30 дней {fmt_usd(s.month.cost)}",
    ])
    details = [
        f"{theme.BULLET} Сегодня: {s.today.requests} запр. · {s.today.sessions} сесс.",
        f"{theme.BULLET} Вчера: {fmt_usd(s.yesterday.cost)} · {s.yesterday.requests} запр.",
        f"{theme.BULLET} В среднем за 7 дней: {fmt_usd(s.week.cost / 7)}/день",
        f"{theme.BULLET} Всё время: {fmt_usd(s.all_time.cost)}",
    ]
    if s.unpriced_models:
        details.append(f"{theme.BULLET} Нет цен для: {esc_fit(', '.join(s.unpriced_models), 200)} (не учтены)")
    details += ["", "<b>Что расходует лимиты (24 ч)</b>"]
    if not d.requests:
        details.append("<i>За сутки запросов не было</i>")
    else:
        details.append(
            f"{theme.BULLET} {d.heavy_share:.0%} запросов с контекстом &gt;{fmt_tokens(d.threshold)} токенов "
            f"({d.heavy_requests} из {d.requests}, {d.heavy_cost_share:.0%} стоимости)"
        )
        if d.model_shares:
            shares = theme.SEP.join(
                f"{esc(theme.model_name(name) or name)} {share:.0%}" for name, share in d.model_shares[:4]
            )
            details.append(f"{theme.BULLET} Модели: {shares}")
    tip = theme.item("💡 Длинный контекст дорог: 🧹 сожмите чат или начните 🆕 чистый") if d.heavy_share >= 0.5 else ""
    return theme.lines(head, main, tip, theme.quote("\n".join(details)))


def render_dashboard(
    quota: QuotaResult | None, summary: UsageSummary, view: SessionView | None, selected: bool,
    quota_enabled: bool = True, now: float | None = None,
) -> str:
    now = time.time() if now is None else now
    stamp = _local(now).strftime("%d.%m %H:%M")
    return fit_html(theme.join_blocks(
        theme.lines(theme.title("Лимиты и расход"), theme.meta(stamp, _utc_offset())),
        render_quota(quota, now, quota_enabled),
        render_session(view, now, selected),
        render_costs(summary),
    ), TG_SAFE_LIMIT)


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
        bar = theme.BAR_ON * filled + theme.BAR_OFF * (DAY_BAR_WIDTH - filled)
        rows.append(f"{day_label(d)} {bar} {fmt_usd(d.cost):>9} {d.requests:>4} запр.")
    total = sum(d.cost for d in days)
    tokens = Tokens()
    for d in days:
        tokens.add(d.tokens)
    active = [d for d in days if d.requests]
    facts = [f"Итого: <b>{fmt_usd(total)}</b> · в среднем {fmt_usd(total / n)}/день · активных дней: {len(active)}"]
    if active:
        top = max(active, key=lambda d: d.cost)
        facts.append(f"Пик: {day_label(top)} · {fmt_usd(top.cost)} · {top.requests} запр.")
    facts.append(theme.meta(f"Токены: {fmt_tokens(tokens.total)} ({render_tokens(tokens)})"))
    return fit_html(theme.join_blocks(
        theme.lines(theme.title("Расход по дням"), theme.meta(f"последние {n} дн.", "сегодня сверху")),
        "<pre>" + esc("\n".join(rows)) + "</pre>",
        theme.lines(*facts),
    ), TG_SAFE_LIMIT)


def _top_lines(label: str, items: list[TopItem], project: bool) -> str:
    rows = [theme.section(label)]
    if not items:
        rows.append("<i>нет запусков</i>")
    for i, entry in enumerate(items, 1):
        name = theme.folder_label(entry.label) if project else entry.label
        shown = esc(truncate(name, 60))
        shown = f"<code>{shown}</code>" if project else f"«{shown}»"
        rows.append(f"{i}. {shown}: <b>{fmt_usd(entry.cost)}</b> · {entry.requests} запр.")
    return "\n".join(rows)


def render_top(projects_7: list[TopItem], chats_7: list[TopItem], projects_30: list[TopItem],
               chats_30: list[TopItem]) -> str:
    return fit_html(theme.join_blocks(
        theme.lines(theme.title("Самые дорогие проекты и чаты"), theme.meta("по ценам API")),
        _top_lines("Проекты · 7 дней", projects_7, project=True),
        _top_lines("Чаты · 7 дней", chats_7, project=False),
        _top_lines("Проекты · 30 дней", projects_30, project=True),
        _top_lines("Чаты · 30 дней", chats_30, project=False),
    ), TG_SAFE_LIMIT)
