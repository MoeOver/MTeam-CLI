"""保活防线：通知事件过滤、lastLogin 确认、间隔调度、状态提醒。"""

import asyncio
import logging
import textwrap
from datetime import datetime, timedelta

import pytest

from mteam_cli.api.humanize import SITE_TZ
from mteam_cli.automation.login import login_recorded
from mteam_cli.automation.remind import compose_reminder
from mteam_cli.core.config import NOTIFY_EVENTS, Settings
from mteam_cli.notify.base import Notification, NotificationEvent, NotifierHub
from mteam_cli.scheduler.daily import keepalive_due

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=SITE_TZ)


def _settings(tmp_path, body: str) -> Settings:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return Settings.from_toml(p)


# ── 配置 ──

def test_new_keys_default_to_legacy_behaviour(tmp_path):
    s = _settings(tmp_path, """
        [[account]]
        username = "u"
    """)
    a = s.accounts[0]
    assert a.notify_events == NOTIFY_EVENTS
    assert a.force_password_login is False
    assert (s.schedule_every_days, s.schedule_remind_day) == (1, 0)


def test_new_keys_parsed(tmp_path):
    s = _settings(tmp_path, """
        [schedule]
        every_days = 7
        remind_day = 1
        [[account]]
        username = "u"
        force_password_login = true
          [account.notify]
          events = ["checkin_failed", "remind"]
    """)
    a = s.accounts[0]
    assert a.notify_events == ("checkin_failed", "remind")
    assert a.force_password_login is True
    assert (s.schedule_every_days, s.schedule_remind_day) == (7, 1)


@pytest.mark.parametrize("schedule_body", ["every_days = 0", "every_days = 31", "remind_day = 29"])
def test_out_of_range_schedule_rejected(tmp_path, schedule_body):
    with pytest.raises(SystemExit):
        _settings(tmp_path, f"""
            [schedule]
            {schedule_body}
            [[account]]
            username = "u"
        """)


def test_unknown_event_rejected(tmp_path):
    with pytest.raises(SystemExit, match="notify.events"):
        _settings(tmp_path, """
            [[account]]
            username = "u"
              [account.notify]
              events = ["failed"]
        """)


def test_config_events_match_notification_enum():
    assert set(NOTIFY_EVENTS) == {e.value for e in NotificationEvent}


# ── 通知过滤 ──

class _Recorder:
    name = "rec"

    def __init__(self):
        self.sent = []

    async def send(self, n):
        self.sent.append(n.event)


def test_hub_only_delivers_subscribed_events():
    rec = _Recorder()
    hub = NotifierHub([rec], logging.getLogger("t"), events=("checkin_failed",))
    for ev in NotificationEvent:
        asyncio.run(hub.notify(Notification(event=ev, title="t", body="b")))
    assert rec.sent == [NotificationEvent.CHECKIN_FAILED]


# ── lastLogin 确认 ──

def _profile(last_login):
    return {"data": {"memberStatus": {"lastLogin": last_login}}}


def test_login_recorded_requires_last_login_after_start():
    assert login_recorded(_profile("2026-10-10 12:01:00"), NOW)
    assert login_recorded(_profile("2026-10-10 11:55:00"), NOW)  # within clock-skew tolerance
    assert not login_recorded(_profile("2026-10-10 11:30:00"), NOW)  # stale login = site didn't record us
    assert not login_recorded(_profile(None), NOW)
    assert not login_recorded({}, NOW)


# ── 间隔调度 ──

def test_keepalive_due_respects_interval_with_half_day_slack():
    assert keepalive_due(None, NOW, 7)
    assert not keepalive_due(NOW - timedelta(days=6), NOW, 7)
    assert keepalive_due(NOW - timedelta(days=6, hours=13), NOW, 7)
    assert keepalive_due(NOW - timedelta(hours=13), NOW, 1)


# ── 状态提醒 ──

def test_reminder_reports_deadline_and_flags_stale_login():
    title, body = compose_reminder("u", _profile("2026-10-01 09:00:00")["data"], None, NOW, 7)
    assert "每月保号提醒" in title
    assert "2026-11-10 09:00" in body  # lastLogin + 40 天

    title, _ = compose_reminder("u", _profile("2026-09-29 09:00:00")["data"], None, NOW, 7)
    assert "保活可能已失效" in title  # > 7 天 + 3 天宽限


def test_reminder_without_site_data_falls_back_to_local_success():
    title, body = compose_reminder("u", None, NOW - timedelta(days=2), NOW, 7)
    assert "每月保号提醒" in title and "无法给出删号截止日" in body
    title, _ = compose_reminder("u", None, None, NOW, 7)
    assert "保活可能已失效" in title
