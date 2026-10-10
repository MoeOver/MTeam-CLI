"""Keep-alive browser login for one account.

Preserves the proven login logic from the original script:
  1. Try restoring the per-account localStorage snapshot (skips 2FA) — unless
     ``force_password_login`` is set.
  2. Fall back to username + password + TOTP, handling the observed 2FA
     variants (direct redirect / OTP input / confirm button).
  3. Confirm success by intercepting the ``/api/member/profile`` XHR and
     matching ``data.username`` against the configured account (and not being
     left on ``/login``). On success the localStorage snapshot is saved.
  4. After a password login, require the site's own ``memberStatus.lastLogin``
     (from that same intercepted profile) to have moved to this run. That is
     the only field API-key traffic cannot touch (``lastBrowse`` moves on any
     API call), so it is the proof the site actually recorded a login.

On failure a screenshot + HTML snapshot is saved under ``artifact_dir`` so a
site redesign can be diagnosed from the artifacts alone.

TOTP makes this fully automatic — there is no QR / human-in-the-loop.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pyotp
from playwright.async_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from mteam_cli.api.humanize import SITE_TZ, parse_site_time
from mteam_cli.automation.localstorage import LocalStorageManager
from mteam_cli.core.browser import BrowserSession
from mteam_cli.core.config import Account, Settings
from mteam_cli.core.models import CheckinResult

_PROFILE_ENDPOINT = "/api/member/profile"
# Tolerance for clock skew between this host and the site when checking lastLogin.
_LAST_LOGIN_SKEW = timedelta(minutes=10)

# Selectors kept deliberately loose so cosmetic redesigns (繁/简/English copy,
# new ids) don't break the flow; the profile-XHR check is the real success test.
_OTP_INPUT = (
    'input#otp-code, input[autocomplete="one-time-code"], '
    'input[placeholder*="驗證碼"], input[placeholder*="验证码"], input[placeholder*="code" i]'
)
_CONFIRM_BUTTON = (
    'button:has-text("確認"), button:has-text("确认"), button:has-text("確定"), '
    'button:has-text("确定"), button:has-text("Confirm")'
)


class _LoginAborted(Exception):
    """Internal: a login strategy did not achieve an authenticated session."""


def login_recorded(profile: dict[str, Any], started: datetime) -> bool:
    """True if the site's ``lastLogin`` is at/after ``started`` (minus skew)."""
    data = profile.get("data") if profile else None
    status = (data or {}).get("memberStatus") or {}
    last = parse_site_time(status.get("lastLogin"))
    return last is not None and last >= started - _LAST_LOGIN_SKEW


async def perform_login(
    session: BrowserSession,
    account: Account,
    settings: Settings,
    logger: logging.Logger,
) -> CheckinResult:
    """Run the full keep-alive login for ``account`` and return its outcome."""
    page = session.page
    if page is None:
        raise RuntimeError("Browser session has not been started.")

    login_url = f"{settings.base_url}/login"
    storage_file = str(account.storage_path(settings.auth_dir))
    started = datetime.now(SITE_TZ)

    # Shared holder populated by the intercepted profile XHR.
    profile: dict[str, Any] = {}

    async def _intercept(route, request) -> None:
        if request.url.endswith(_PROFILE_ENDPOINT):
            logger.info("[%s] 命中 profile 请求: %s", account.username, request.url)
            try:
                response = await route.fetch()
                profile.clear()
                profile.update(await response.json())
                await route.fulfill(response=response)
                return
            except (ValueError, PlaywrightError) as exc:
                logger.warning("[%s] 读取 profile 响应出错: %s", account.username, exc)
        await route.continue_()

    await page.route(f"**{_PROFILE_ENDPOINT}", _intercept)
    logger.info("[%s] 已设置 profile 请求拦截", account.username)

    lsm = LocalStorageManager(page)

    def _logged_in() -> bool:
        data = profile.get("data") if profile else None
        return bool(
            data
            and data.get("username") == account.username
            and not urlparse(page.url).path.startswith("/login")
        )

    try:
        await session.goto(f"{settings.base_url}/")

        # ── Strategy 1: localStorage (skipped when a real login is required) ──
        try:
            if account.force_password_login:
                raise _LoginAborted("配置要求每次账号密码登录")
            await _login_by_localstorage(
                page, lsm, storage_file, account, settings, logger, _logged_in
            )
        except _LoginAborted as why:
            logger.info("[%s] 改用账号密码登录（%s）", account.username, why)
            profile.clear()
            await _login_by_password(
                page, lsm, storage_file, login_url, account, settings, logger, _logged_in
            )
            if not login_recorded(profile, started):
                status = ((profile.get("data") or {}).get("memberStatus") or {})
                raise _LoginAborted(
                    f"站点未记录本次登录（lastLogin={status.get('lastLogin')}）"
                )

        text = _parse_profile(profile, account)
        logger.info("[%s] 登录成功", account.username)
        return CheckinResult(username=account.username, ok=True, profile_text=text)

    except (_LoginAborted, PlaywrightError) as exc:
        kind = "登录失败" if isinstance(exc, _LoginAborted) else "Playwright 错误"
        logger.error("[%s] %s: %s", account.username, kind, exc)
        error = f"{kind}: {exc}"
        try:
            bundle = await session.snapshot(f"login-failed-{account.safe_name}", {"error": error})
            error += f"\n现场快照: {bundle.screenshot_path}"
        except Exception as snap_exc:  # noqa: BLE001 — diagnostics must not mask the failure
            logger.warning("[%s] 保存失败快照出错: %s", account.username, snap_exc)
        return CheckinResult(username=account.username, ok=False, error=error)
    finally:
        try:
            await page.unroute(f"**{_PROFILE_ENDPOINT}")
        except PlaywrightError:
            pass


async def _login_by_localstorage(
    page: Page,
    lsm: LocalStorageManager,
    storage_file: str,
    account: Account,
    settings: Settings,
    logger: logging.Logger,
    logged_in,
) -> None:
    logger.info("[%s] 尝试通过 LocalStorage 登录", account.username)
    if not Path(storage_file).exists():
        raise _LoginAborted("无 LocalStorage 快照")
    try:
        await lsm.load_from_file(storage_file)
        await page.reload(timeout=settings.timeout_ms)
        await page.wait_for_load_state("networkidle", timeout=settings.timeout_ms)
        # Give the SPA time to fire its profile XHR (matches legacy behavior).
        await page.wait_for_timeout(timeout=min(settings.timeout_ms, 60_000))
    except PlaywrightError as exc:
        raise _LoginAborted(f"LocalStorage 登录出错: {exc}") from exc

    if logged_in():
        await lsm.save_to_file(storage_file)
        logger.info("[%s] LocalStorage 登录成功，已刷新快照", account.username)
        return
    raise _LoginAborted("LocalStorage 登录未确认")


async def _type_like_human(page: Page, selector: str, text: str) -> None:
    field = page.locator(selector)
    await field.click()
    await field.fill("")
    await field.press_sequentially(text, delay=random.randint(60, 160))
    await page.wait_for_timeout(random.randint(300, 900))


async def _login_by_password(
    page: Page,
    lsm: LocalStorageManager,
    storage_file: str,
    login_url: str,
    account: Account,
    settings: Settings,
    logger: logging.Logger,
    logged_in,
) -> None:
    logger.info("[%s] 尝试通过账号密码登录", account.username)
    try:
        if urlparse(page.url).path != urlparse(login_url).path:
            await page.goto(login_url, timeout=settings.timeout_ms)
        await page.wait_for_load_state("networkidle", timeout=settings.timeout_ms)

        await page.locator('button[type="submit"]').wait_for(timeout=settings.timeout_ms)
        await _type_like_human(page, "input#username", account.username)
        await _type_like_human(page, "input#password", account.password or "")
        await page.locator('button[type="submit"]').click()

        await _handle_2fa(page, account, logger)
        # Let the SPA land and fire its profile XHR before judging.
        await page.wait_for_load_state("networkidle", timeout=settings.timeout_ms)
        for _ in range(15):
            if logged_in():
                break
            await page.wait_for_timeout(1_000)
    except PlaywrightError as exc:
        raise _LoginAborted(f"账号密码登录出错: {exc}") from exc

    if logged_in():
        await lsm.save_to_file(storage_file)
        logger.info("[%s] 账号密码登录成功，已保存快照", account.username)
        return
    raise _LoginAborted("账号密码登录未确认")


def _left_login_page(url: str) -> bool:
    return not urlparse(url).path.startswith("/login")


async def _handle_2fa(page: Page, account: Account, logger: logging.Logger) -> None:
    """Handle the observed 2FA variants after submitting credentials."""
    try:
        await page.wait_for_url(_left_login_page, timeout=15_000)
        logger.info("[%s] 登录直接成功，无需 2FA", account.username)
        return
    except PlaywrightTimeoutError:
        pass

    try:
        otp_input = page.locator(_OTP_INPUT).first
        if await otp_input.count() > 0 and await otp_input.is_visible():
            await otp_input.click()
            await otp_input.press_sequentially(
                pyotp.TOTP(account.totp_secret).now(), delay=random.randint(80, 180)
            )
            await page.locator('button[type="submit"]').first.click()
            await page.wait_for_url(_left_login_page, timeout=30_000)
            return

        confirm_btn = page.locator(_CONFIRM_BUTTON).first
        if await confirm_btn.count() > 0:
            await confirm_btn.click()
            await page.wait_for_url(_left_login_page, timeout=30_000)
            return

        logger.warning("[%s] 未找到 2FA 元素，等待页面跳转", account.username)
        await page.wait_for_timeout(5_000)
    except PlaywrightTimeoutError as exc:
        logger.warning("[%s] 处理 2FA 超时: %s", account.username, exc)
    except PlaywrightError as exc:
        logger.warning("[%s] 处理 2FA 出错: %s", account.username, exc)


def _parse_profile(profile: dict[str, Any], account: Account) -> str:
    """Format the intercepted profile into a notification body (legacy parity)."""
    from mteam_cli.api import humanize as hz

    data: dict[str, Any] | None = profile.get("data") if profile else None
    if not data:
        return "获取到的数据为空"

    lines = [
        f"用户ID: {data.get('id')}",
        f"用户名: {data.get('username')}",
        f"用户Email: {data.get('email')}",
        f"登录IP: {data.get('ip')}",
        f"账户创建时间: {data.get('createdDate')}",
        f"账户更新时间: {data.get('lastModifiedDate')}",
    ]

    status = data.get("memberStatus")
    if status:
        lines += [
            f"会员最新登录时间: {status.get('lastLogin')}",
            f"会员最新浏览时间: {status.get('lastBrowse')}",
        ]

    count = data.get("memberCount")
    if count:
        lines += [
            f"上传量: {hz.naturalsize(count.get('uploaded'))}",
            f"下载量: {hz.naturalsize(count.get('downloaded'))}",
            f"魔力值: {hz.num(count.get('bonus'))}",
            f"分享率: {hz.ratio(count.get('shareRate'))}",
        ]

    return "\n".join(lines)
