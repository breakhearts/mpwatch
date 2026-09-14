"""Resolve an explicitly selected account and verify remote/local subscription."""

import base64
import os
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

import httpx
from bs4 import BeautifulSoup

from .collector import CollectError


class SubscriptionError(Exception):
    def __init__(self, kind, **details):
        super().__init__(kind)
        self.details = {"kind": kind, **details}


def validate_book_id(value):
    if not re.fullmatch(r"MP_WXS_[0-9]+", value):
        raise SubscriptionError("invalid_book_id")
    return value


def browser_article_biz(url):
    """Use a normal fresh browser for the public page, never a CAPTCHA solver."""
    try:
        from playwright.sync_api import Error as BrowserError
        from playwright.sync_api import TimeoutError as BrowserTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SubscriptionError("article_browser_required", hint="uv sync --extra auth") from None
    try:
        with sync_playwright() as playwright:
            options = {"headless": True}
            if os.name == "nt":
                options["channel"] = "msedge"
            browser = playwright.chromium.launch(**options)
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    page.wait_for_selector("#js_content, .rich_media_content", timeout=10000)
                except BrowserTimeout:
                    raise SubscriptionError(
                        "article_requires_verification",
                        hint="open the article in WeChat and provide a full __biz link",
                    ) from None
                if urlsplit(page.url).hostname != "mp.weixin.qq.com":
                    raise SubscriptionError("invalid_article_redirect")
                biz = page.evaluate("() => typeof window.biz === 'string' ? window.biz : null")
                if not biz:
                    raise SubscriptionError("article_account_not_found")
                return biz
            finally:
                browser.close()
    except BrowserError:
        raise SubscriptionError("article_browser_failed") from None


def article_book_id(url, *, client=None):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "mp.weixin.qq.com"
        or parsed.netloc != "mp.weixin.qq.com"
        or parsed.path not in {"/s", "/s/"}
        and not re.fullmatch(r"/s/[A-Za-z0-9_~\-]+", parsed.path)
    ):
        raise SubscriptionError("invalid_article_url")
    values = parse_qs(parsed.query).get("__biz", [])
    if not values:
        # The article host never receives WeRead credentials, and redirects are not followed.
        owned = client is None
        client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
        try:
            response = client.get(url)
        except httpx.RequestError:
            raise SubscriptionError("article_network_error") from None
        finally:
            if owned:
                client.close()
        redirect = urlsplit(response.headers.get("location", ""))
        if (
            300 <= response.status_code < 400
            and redirect.hostname in {None, "mp.weixin.qq.com"}
            and redirect.path == "/mp/wappoc_appmsgcaptcha"
        ):
            values = [browser_article_biz(url)]
        elif response.status_code != 200:
            raise SubscriptionError("article_unavailable", code=response.status_code)
        else:
            soup = BeautifulSoup(response.text, "html.parser")
            match = re.search(
                r"\b(?:var\s+)?biz\s*=\s*[\"\x27]([A-Za-z0-9+/=]+)[\"\x27]", response.text
            )
            if soup.select_one("#js_content, .rich_media_content") is not None and match:
                values = [match.group(1)]
            else:
                values = [browser_article_biz(url)]
    if len(values) != 1:
        raise SubscriptionError("invalid_article_biz")
    try:
        account_id = base64.b64decode(values[0], validate=True).decode("ascii")
    except (ValueError, UnicodeError):
        raise SubscriptionError("invalid_article_biz") from None
    return validate_book_id("MP_WXS_" + account_id)


def resolve(collector, *, book_id=None, name=None, article_url=None):
    if book_id:
        return collector.account(validate_book_id(book_id))
    if article_url:
        return collector.account(article_book_id(article_url))
    if not name or not name.strip():
        raise SubscriptionError("empty_name")
    candidates = collector.search_accounts(name.strip())
    exact = [c for c in candidates if c["name"] == name.strip()]
    if len(exact) != 1:
        raise SubscriptionError(
            "account_selection_required",
            candidates=candidates,
            hint="select an exact --book-id from search results",
        )
    validate_book_id(exact[0]["book_id"])
    return collector.account(exact[0]["book_id"])


def subscribe(store, collector, account, *, dry_run=False):
    book_id = validate_book_id(account["book_id"])
    before = {s["book_id"] for s in collector.sources()}
    if dry_run:
        return {
            "status": "preview",
            "account": account,
            "already_on_shelf": book_id in before,
            "actions": (["save_local"] if book_id in before else ["add_to_shelf", "save_local"]),
        }
    remote = "already_subscribed"
    if book_id not in before:
        uncertain = None
        try:
            collector.add_to_shelf(book_id)
        except CollectError as error:
            if error.kind != "mutation_unknown":
                raise
            uncertain = error.as_dict()
        try:
            after = {s["book_id"] for s in collector.sources()}
        except CollectError as error:
            raise SubscriptionError(
                "subscription_unverified",
                remote="unknown",
                local="unchanged",
                cause=error.as_dict(),
            ) from None
        if book_id not in after:
            raise SubscriptionError(
                "subscription_unverified",
                remote="not_observed",
                local="unchanged",
                cause=uncertain,
                hint="retry later; no local monitor was enabled",
            )
        remote = "subscribed"
    try:
        store.add(book_id, account["name"], True)
    except (sqlite3.Error, OSError):
        raise SubscriptionError(
            "local_save_failed",
            remote=remote,
            local="failed",
            account=account,
            hint="retry to save the already subscribed account",
        ) from None
    return {"status": "subscribed", "account": account, "remote": remote, "local": "enabled"}
