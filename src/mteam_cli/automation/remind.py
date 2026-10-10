"""Periodic account-status reminder — no browser.

A keep-alive that silently stops working is the failure mode that actually
loses accounts (40 days without a real login → deleted). The reminder reports
the site's own ``lastLogin``, the resulting deletion deadline and VIP expiry
(via the API key, when configured) plus the last local keep-alive success, and
escalates the title when the last real login is older than the keep-alive
interval allows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from mteam_cli.api import get_profile
from mteam_cli.api.humanize import SITE_TZ, parse_site_time
from mteam_cli.core.config import Account, Settings
from mteam_cli.notify import Notification, NotificationEvent, build_notifier_hub

DELETE_AFTER = timedelta(days=40)  # M-Team: 40 consecutive days without login → account deleted
_GRACE = timedelta(days=3)  # pre-delay jitter, missed runs, a transient failure


def compose_reminder(
    username: str,
    profile_data: dict[str, Any] | None,
    last_ok: datetime | None,
    now: datetime,
    every_days: int,
) -> tuple[str, str]:
    """Return ``(title, body)``. Stale = no real login seen within interval + grace."""
    status = (profile_data or {}).get("memberStatus") or {}
    last_login = parse_site_time(status.get("lastLogin"))
    evidence = last_login or last_ok
    stale = evidence is None or now - evidence > timedelta(days=every_days) + _GRACE

    def fmt(t: datetime | None) -> str:
        return t.astimezone(SITE_TZ).strftime("%Y-%m-%d %H:%M") if t else "未知"

    lines = [f"本地最近一次保活成功：{fmt(last_ok)}"]
    if profile_data is not None:
        lines += [
            f"站点记录的最后登录：{fmt(last_login)}",
            f"删号截止（最后登录 + 40 天）：{fmt(last_login + DELETE_AFTER if last_login else None)}",
            f"VIP 到期：{status.get('vipUntil') or '无'}",
        ]
    else:
        lines.append("（未读到站点资料：未配置 api_key 或接口出错，无法给出删号截止日）")
    lines.append(
        "⚠️ 最近没有成功的真实登录，自动保活可能已失效，请尽快用浏览器手动登录一次。"
        if stale
        else "自动保活工作正常；仍建议偶尔用浏览器手动登录一次。"
    )
    title = f"[{username}] {'保活可能已失效' if stale else '每月保号提醒'}"
    return title, "\n".join(lines)


async def run_remind_tick(account: Account, settings: Settings, logger: logging.Logger) -> int:
    """Send one status reminder for ``account``; returns 0 (reminders never fail a run)."""
    profile_data = None
    if account.can_query:
        try:
            data = await get_profile(account.api_key, base_url=settings.api_base_url)
            profile_data = data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001 — reminder still goes out without site data
            logger.warning("[%s] 提醒：读取 profile 失败: %s", account.username, exc)

    title, body = compose_reminder(
        account.username,
        profile_data,
        account.read_last_ok(settings.auth_dir),
        datetime.now(SITE_TZ),
        settings.schedule_every_days,
    )
    hub = build_notifier_hub(account, settings, logger)
    await hub.notify(Notification(event=NotificationEvent.REMIND, title=title, body=body))
    logger.info("[%s] 提醒：%s", account.username, title)
    return 0
