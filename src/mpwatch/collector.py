"""Small WeRead adapter. Remote writes only occur through explicit subscription."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup


class CollectError(Exception):
    def __init__(self, kind, *, code=None):
        # Never include remote error strings, request headers, or exception reprs.
        super().__init__(kind)
        self.kind = kind
        self.code = code

    @property
    def stop(self):
        return self.kind in {"auth", "blocked", "rate_limited", "budget"}

    def as_dict(self):
        return {"kind": self.kind, "code": self.code}


def obj(value):
    if not isinstance(value, dict):
        raise CollectError("invalid_response")
    return value


def string(value, *, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise CollectError("invalid_response")
    return value.strip()


def validate_payload(payload):
    obj(payload)
    for key in ("errCode", "errcode"):
        if key not in payload:
            continue
        try:
            code = int(payload[key])
        except (TypeError, ValueError, OverflowError):
            raise CollectError("invalid_response") from None
        if code:
            kind = "auth" if code in {-2012, -2010, -2041} else "upstream"
            raise CollectError(kind, code=code)
    return payload


def article_link(book_id, review_id, token=""):
    if not token and review_id.startswith(book_id + "_"):
        token = review_id[len(book_id) + 1 :]
    # Do not split on underscores: article tokens may contain them.
    return "https://mp.weixin.qq.com/s/" + quote(token, safe="~") if token else None


def parse_articles(payload, book_id):
    validate_payload(payload)
    groups = payload.get("reviews")
    if not isinstance(groups, list):
        raise CollectError("invalid_response")
    articles = []
    for group in groups:
        children = obj(group).get("subReviews")
        if not isinstance(children, list) or not children:
            raise CollectError("invalid_response")
        for child in children:
            review = obj(obj(child).get("review"))
            info = obj(review.get("mpInfo"))
            review_id = string(review.get("reviewId") or child.get("reviewId"), required=True)
            token = string(info.get("originalId", ""))
            articles.append(
                {
                    "review_id": review_id,
                    "title": string(info.get("title"), required=True),
                    "url": article_link(book_id, review_id, token),
                    # Keep unverified upstream times as evidence, not publication facts.
                    "published_at": None,
                    "publish_time_source": "unverified",
                    "source_times": {
                        "group_create_time": group.get("createTime"),
                        "review_create_time": review.get("createTime"),
                        "mp_time": info.get("time"),
                    },
                }
            )
    return articles, len(groups)


def extract_content(html):
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#js_content, .rich_media_content")
    if body is None:
        page_text = soup.get_text(" ", strip=True)
        if any(marker in page_text for marker in ("当前环境异常", "完成验证", "登录微信读书")):
            raise CollectError("blocked")
        raise CollectError("invalid_content")
    for element in body.select("script, style, iframe, form"):
        element.decompose()
    text = body.get_text("\n", strip=True)
    if not text:
        raise CollectError("invalid_content")
    return {"text": text, "html": str(body)}


@dataclass
class Page:
    articles: list
    groups: int


class Collector:
    def __init__(
        self,
        cookie,
        *,
        client=None,
        clock=time.monotonic,
        sleep=time.sleep,
        interval=2.0,
        budget=300.0,
    ):
        self.client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self.headers = {
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 mpwatch/0.1",
            "Referer": "https://weread.qq.com/",
            "Accept": "*/*",
        }
        self.clock, self.sleep, self.interval = clock, sleep, interval
        self.deadline = clock() + budget
        self.last_request = None

    def close(self):
        self.client.close()

    def _wait(self, delay):
        if delay >= self.deadline - self.clock():
            raise CollectError("budget")
        if delay > 0:
            self.sleep(delay)

    def _get(self, path, params, *, html=False):
        for attempt in range(3):
            if self.last_request is not None:
                self._wait(max(0, self.interval - (self.clock() - self.last_request)))
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise CollectError("budget")
            self.last_request = self.clock()
            try:
                response = self.client.get(
                    "https://weread.qq.com" + path,
                    params=params,
                    headers=self.headers,
                    timeout=min(30, remaining),
                )
            except httpx.RequestError:
                if attempt == 2:
                    raise CollectError("network") from None
                self._wait(2**attempt)
                continue
            status = response.status_code
            if status == 401:
                raise CollectError("auth", code=status)
            if status == 403 or 300 <= status < 400:
                raise CollectError("blocked", code=status)
            if status == 429:
                if attempt == 2:
                    raise CollectError("rate_limited", code=status)
                retry = response.headers.get("Retry-After", "2")
                try:
                    delay = float(retry)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry) - datetime.now(UTC)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        delay = 2
                if not 0 <= delay < self.deadline - self.clock():
                    raise CollectError("rate_limited", code=status)
                self._wait(delay)
                continue
            if status >= 500 and attempt < 2:
                self._wait(2**attempt)
                continue
            if status != 200:
                raise CollectError("http", code=status)
            if self.clock() >= self.deadline:
                raise CollectError("budget")
            if html:
                if response.text.lstrip().startswith("{"):
                    self._json(response)
                    raise CollectError("invalid_content")
                return response.text
            return self._json(response)
        raise CollectError("network")

    @staticmethod
    def _json(response):
        try:
            payload = response.json()
        except ValueError:
            # HTML from a JSON endpoint can be a login or challenge page.
            kind = "blocked" if "<html" in response.text.lower() else "invalid_json"
            raise CollectError(kind) from None
        return validate_payload(payload)

    def page(self, book_id, offset):
        try:
            payload = self._get("/web/mp/articles", {"bookId": book_id, "offset": offset})
        except CollectError as error:
            if error.code != -2041:
                raise
            # Real probe: list -2041 can coexist with a working shelf, cover and body.
            # Only reclassify this endpoint after independently verifying authentication.
            self.sources()
            raise CollectError("list_unavailable", code=-2041) from None
        articles, groups = parse_articles(payload, book_id)
        return Page(articles, groups)

    def cover(self, book_id):
        payload = self._get("/api/mp/cover", {"bookId": book_id})
        review_id = string(payload.get("reviewId"), required=True)
        return {
            "review_id": review_id,
            "title": string(payload.get("title"), required=True),
            "url": article_link(book_id, review_id),
            "published_at": None,
            "publish_time_source": "unknown",
            "source_times": {},
        }

    def account(self, book_id):
        payload = self._get("/api/mp/cover", {"bookId": book_id})
        return {"book_id": book_id, "name": string(payload.get("name"), required=True)}

    def search_accounts(self, keyword):
        # Current official web client starts a search session before requesting a scope.
        initial = self._get("/api/store/search", {"keyword": keyword})
        sid = string(initial.get("sid"), required=True)
        try:
            payload = self._get(
                "/api/store/search",
                {"keyword": keyword, "sid": sid, "scope": 2, "maxIdx": 0, "count": 20},
            )
        except CollectError as error:
            if error.kind == "http" and error.code == 499:
                raise CollectError("account_search_unavailable", code=499) from None
            raise
        # Known web envelope: results contains typed groups; do not use ordinary books.
        groups = payload.get("results")
        if not isinstance(groups, list):
            raise CollectError("invalid_response")
        accounts = {}
        for group in groups:
            obj(group)
            if group.get("scope") != 2:
                continue
            books = group.get("books")
            if not isinstance(books, list):
                raise CollectError("invalid_response")
            for book in books:
                info = obj(obj(book).get("bookInfo"))
                book_id = string(info.get("bookId"), required=True)
                if book_id.startswith("MP_WXS_"):
                    accounts[book_id] = {
                        "book_id": book_id,
                        "name": string(info.get("title"), required=True),
                    }
        return list(accounts.values())

    def add_to_shelf(self, book_id):
        # Never blindly retry a mutation. The caller verifies the shelf afterwards.
        if self.last_request is not None:
            self._wait(max(0, self.interval - (self.clock() - self.last_request)))
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise CollectError("budget")
        self.last_request = self.clock()
        try:
            response = self.client.post(
                "https://weread.qq.com/web/shelf/add",
                json={"bookIds": [book_id]},
                headers={**self.headers, "Origin": "https://weread.qq.com"},
                timeout=min(30, remaining),
            )
        except httpx.RequestError:
            raise CollectError("mutation_unknown") from None
        if response.status_code == 401:
            raise CollectError("auth", code=401)
        if response.status_code == 403 or 300 <= response.status_code < 400:
            raise CollectError("blocked", code=response.status_code)
        if response.status_code == 429:
            raise CollectError("rate_limited", code=429)
        if response.status_code != 200:
            raise CollectError("mutation_unknown", code=response.status_code)
        try:
            return self._json(response)
        except CollectError as error:
            if error.kind in {"invalid_json", "invalid_response"}:
                raise CollectError("mutation_unknown") from None
            raise

    def content(self, review_id):
        html = self._get("/web/mp/content", {"reviewId": review_id}, html=True)
        return extract_content(html)

    def sources(self):
        payload = self._get("/web/shelf/sync", {"userVid": "", "synckey": 0, "lectureSynckey": 0})
        books = payload.get("books")
        if not isinstance(books, list):
            raise CollectError("invalid_response")
        result = []
        for book in books:
            book_id = string(obj(book).get("bookId"), required=True)
            if book_id.startswith("MP_WXS_"):
                result.append(
                    {"book_id": book_id, "name": string(book.get("title"), required=True)}
                )
        return result
