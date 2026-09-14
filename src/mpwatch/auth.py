"""Interactive first-party QR login, with verified atomic cookie persistence."""

import os
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from filelock import FileLock

from .collector import CollectError, Collector


class AuthError(Exception):
    """Safe, fixed error identifier; never contains browser or credential details."""


def verify(cookie):
    with closing(Collector(cookie, budget=30)) as collector:
        sources = collector.sources()
    return {
        "status": "authenticated",
        "source_count": len(sources),
        "verification": "weread_shelf",
        "sources": sources,
    }


def cookie_header(cookies):
    values = {}
    for cookie in cookies:
        # Input comes from cookies scoped to the first-party shelf URL in a fresh context.
        name, value = cookie["name"], cookie["value"]
        if any(char in name + value for char in "\r\n;"):
            raise AuthError("invalid_browser_cookie")
        values[name] = value
    if not values.get("wr_skey") or not values.get("wr_vid"):
        return None
    return "; ".join(f"{name}={value}" for name, value in sorted(values.items()))


def save_cookie(path, cookie):
    """A failed write never replaces an existing working login."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".mpwatch-auth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(cookie)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def wait_for_login(
    context,
    page,
    path,
    *,
    timeout=300,
    clock=time.monotonic,
    sleep=time.sleep,
    verifier=verify,
    progress=None,
):
    deadline = clock() + timeout
    last_candidate = None
    last_attempt = float("-inf")
    last_error = None
    while clock() < deadline:
        if page.is_closed():
            raise AuthError("login_window_closed")
        candidate = cookie_header(context.cookies("https://weread.qq.com/web/shelf/sync"))
        if candidate and (candidate != last_candidate or clock() - last_attempt >= 15):
            last_candidate, last_attempt = candidate, clock()
            if progress:
                progress("检测到登录态，正在验证公众号书架接口……")
            try:
                result = verifier(candidate)
            except CollectError as error:
                last_error = error.kind
                if error.kind in {"blocked", "rate_limited"}:
                    raise AuthError("login_verification_" + error.kind) from None
                # Auth may be incomplete while the redirect is still settling.
            else:
                save_cookie(path, candidate)
                return {**result, "cookie_file": str(path)}
        sleep(min(1, max(0, deadline - clock())))
    if last_error:
        raise AuthError("login_verification_" + last_error)
    raise AuthError("login_timeout")


def login(path, *, browser="msedge", timeout=300, progress=None):
    try:
        from playwright.sync_api import Error as BrowserError
        from playwright.sync_api import TimeoutError as BrowserTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise AuthError("install_auth_extra_uv_sync_extra_auth") from None

    if progress is None:
        progress = lambda message: print(message, file=sys.stderr, flush=True)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Separate from the data writer lock: reading reports does not wait for a scan.
    with FileLock(path.with_name(path.name + ".lock"), timeout=0):
        try:
            with sync_playwright() as playwright:
                options = {"headless": False}
                if browser != "chromium":
                    options["channel"] = browser
                instance = playwright.chromium.launch(**options)
                try:
                    context = instance.new_context(viewport={"width": 1100, "height": 850})
                    page = context.new_page()
                    page.goto(
                        "https://weread.qq.com/", wait_until="domcontentloaded", timeout=30000
                    )
                    try:
                        # Public homepage login link, inspected on 2026-09-14.
                        page.get_by_text("登录", exact=True).first.click(timeout=10000)
                    except BrowserTimeout:
                        progress("请在打开的微信读书页面点击登录，完成扫码。")
                    progress("请使用微信扫描浏览器中的登录二维码，并在手机上确认。")
                    return wait_for_login(context, page, path, timeout=timeout, progress=progress)
                finally:
                    instance.close()
        except BrowserError:
            raise AuthError("login_browser_error_check_browser_installation_or_window") from None
