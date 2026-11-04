"""Notifications via Apprise (ntfy, Discord, Telegram, email and ~100 more).

Failures are logged and never break a run.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from signal_news.config import Config

log = logging.getLogger(__name__)

EMAIL_SCHEMES = ("mailto://", "mailtos://")
_WIKI_WITH_URL = re.compile(r"\[\[[^\]|]+\|([^\]]+)\]\]\s*\[↗\]\(([^)]+)\)")
_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def notification_urls(cfg: Config) -> list[str]:
    """URLs from config plus the ``APPRISE_URLS`` environment variable (comma/space/newline separated)."""
    urls = list(cfg.notifications.urls)
    env = os.environ.get(cfg.notifications.urls_env, "")
    urls += [u for u in re.split(r"[\s,]+", env) if u]
    seen: list[str] = []
    for u in urls:
        if u not in seen:
            seen.append(u)
    return seen


def to_message_markdown(text: str) -> str:
    """Make digest Markdown suitable for chat apps: wikilinks become plain links to the original."""
    text = _WIKI_WITH_URL.sub(r"[\1](\2)", text)
    return _WIKI.sub(lambda m: m.group(2) or m.group(1).rsplit("/", 1)[-1], text)


def truncate(text: str, limit: int, suffix: str = "") -> str:
    """Cut at a line boundary so the message plus ``suffix`` fits in ``limit`` characters."""
    if len(text) + len(suffix) <= limit:
        return text + suffix
    room = max(0, limit - len(suffix) - 2)
    cut = text[:room]
    if "\n" in cut:
        cut = cut.rsplit("\n", 1)[0]
    return cut.rstrip() + "\n…" + suffix


def _send(urls: list[str], title: str, body: str, fmt: str = "markdown") -> tuple[bool, str]:
    if not urls:
        return (
            False,
            "No notification URLs configured (Settings → Notifications, or APPRISE_URLS in .env)",
        )
    try:
        import apprise

        ap = apprise.Apprise()
        bad = [u.split("://", 1)[0] + "://…" for u in urls if not ap.add(u)]
        if len(ap) == 0:
            return False, f"No valid notification URLs ({', '.join(bad)})"
        body_format = apprise.NotifyFormat.HTML if fmt == "html" else apprise.NotifyFormat.MARKDOWN
        ok = ap.notify(title=title, body=body, body_format=body_format)
    except Exception as exc:  # noqa: BLE001
        log.exception("Notification failed")
        return False, f"Notification error: {exc}"
    msg = (
        f"Sent to {len(urls) - len(bad)} target(s)"
        if ok
        else "Sending failed for one or more targets (see logs)"
    )
    if bad:
        msg += f"; invalid: {', '.join(bad)}"
    return bool(ok), msg


def send_digest(cfg: Config, result: Any) -> tuple[bool, str]:
    """Send the Top 5 + Flags to chat targets and the full digest (as HTML) to email targets."""
    urls = notification_urls(cfg)
    if not urls:
        return False, "no targets"
    chat = [u for u in urls if not u.lower().startswith(EMAIL_SCHEMES)]
    email = [u for u in urls if u.lower().startswith(EMAIL_SCHEMES)]
    title = f"Signal digest · {result.date}"
    link = f"\n\nFull digest: {cfg.notifications.ui_base_url.rstrip('/')}/digests/{result.date}"
    ok_all, msgs = True, []
    if chat:
        body = f"**Today's top 5**\n{to_message_markdown(result.top5)}\n\n**Flags**\n{to_message_markdown(result.flags)}"
        ok, msg = _send(chat, title, truncate(body, cfg.notifications.max_message_chars, link))
        ok_all &= ok
        msgs.append(msg)
    if email:
        from signal_news.web.render import render_markdown

        md_text = to_message_markdown(result.markdown)
        html = (
            str(render_markdown(md_text))
            + f'<p><a href="{link.split(": ", 1)[1]}">Open in Signal</a></p>'
        )
        ok, msg = _send(email, title, html, fmt="html")
        ok_all &= ok
        msgs.append(msg)
    log.info("Digest notification: %s", "; ".join(msgs))
    return ok_all, "; ".join(msgs)


def send_instant_alerts(
    cfg: Config, new_kev: list[dict[str, Any]], gear_articles: list[dict[str, Any]]
) -> None:
    """Immediate alerts for newly added KEV entries and articles about your gear."""
    urls = notification_urls(cfg)
    if not urls or not (new_kev or gear_articles):
        return
    chat = [u for u in urls if not u.lower().startswith(EMAIL_SCHEMES)] or urls
    lines: list[str] = []
    for e in new_kev[:10]:
        vendor = f"{e.get('vendorProject', '')} {e.get('product', '')}".strip()
        lines.append(
            f"🚨 **{e['cveID']}** ({vendor}) added to CISA KEV: {e.get('vulnerabilityName', '')}. "
            f"Action: {e.get('requiredAction', '')} https://nvd.nist.gov/vuln/detail/{e['cveID']}"
        )
    if len(new_kev) > 10:
        lines.append(f"…and {len(new_kev) - 10} more KEV entries.")
    for a in gear_articles[:10]:
        lines.append(f"🏠 **Affects your gear:** {a['title']} ({a['source']}) {a['url']}")
    title = "Signal alert"
    if new_kev and not gear_articles:
        title = f"Signal: {len(new_kev)} new actively exploited vulnerabilit{'y' if len(new_kev) == 1 else 'ies'}"
    elif gear_articles and not new_kev:
        title = "Signal: news about your gear"
    ok, msg = _send(chat, title, truncate("\n\n".join(lines), cfg.notifications.max_message_chars))
    log.info("Instant alert (%d KEV, %d gear): %s", len(new_kev), len(gear_articles), msg)


def send_test(cfg: Config) -> tuple[bool, str]:
    """Send a test message to every configured target."""
    return _send(
        notification_urls(cfg),
        "Signal test",
        f"✅ Test notification from Signal. Your digest arrives daily at {cfg.schedule.digest_time} "
        f"({cfg.timezone}).\n\n{cfg.notifications.ui_base_url}",
    )
