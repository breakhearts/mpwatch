"""Single-run orchestration and JSON command interface."""

import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from filelock import Timeout

from .auth import AuthError, login, renew_cookie, verify
from .collector import CollectError, Collector
from .storage import Store, writer
from .subscriptions import SubscriptionError, resolve, subscribe


def refresh(store, collector, *, max_pages=3, max_content=20):
    sources = [s for s in store.sources() if s["enabled"]]
    if not sources:
        raise ValueError("no_enabled_sources")
    run_id = store.start()
    checks = []
    stopped = None
    for source in sources:
        check = {
            "book_id": source["book_id"],
            "name": source["name"],
            "mode": "list",
            "coverage": "failed",
            "coverage_reason": "not_checked",
            "pages": 0,
            "new_count": 0,
            "errors": [],
            "unscanned_articles": "unknown",
        }
        checks.append(check)
        store.check(run_id, check)
        if stopped:
            check["errors"].append({"kind": "skipped", "cause": stopped})
            store.check(run_id, check)
            continue
        offset = 0
        seen_pages = set()
        try:
            for _ in range(max_pages):
                page = collector.page(source["book_id"], offset)
                check["pages"] += 1
                if page.groups == 0:
                    check.update(
                        coverage="complete", coverage_reason="empty_page", unscanned_articles=0
                    )
                    store.check(run_id, check)
                    break
                signature = tuple(a["review_id"] for a in page.articles)
                if signature in seen_pages:
                    raise CollectError("repeated_page")
                seen_pages.add(signature)
                for article in page.articles:
                    check["new_count"] += int(store.discover(run_id, source, article))
                check.update(coverage="partial", coverage_reason="page_limit")
                offset += page.groups
                store.check(run_id, check)
            store.initialize(source["book_id"])
        except CollectError as error:
            check["errors"].append(error.as_dict())
            check["coverage_reason"] = error.kind
            if error.stop:
                stopped = error.kind
            elif check["pages"] == 0:
                try:
                    article = collector.cover(source["book_id"])
                    check["new_count"] += int(store.discover(run_id, source, article))
                    check.update(mode="cover", coverage="partial", coverage_reason="cover_only")
                except CollectError as cover_error:
                    check["errors"].append(cover_error.as_dict())
                    if cover_error.stop:
                        stopped = cover_error.kind
        store.check(run_id, check)

    by_book = {c["book_id"]: c for c in checks}
    if not stopped:
        for article in store.pending(max_content):
            try:
                content = collector.content(article["review_id"])
                store.body(run_id, article["article_id"], content=content)
            except CollectError as error:
                store.body(run_id, article["article_id"], error=error.as_dict())
                by_book[article["book_id"]]["errors"].append(error.as_dict())
                if error.stop:
                    stopped = error.kind
                    break
    for check in checks:
        check["pending_content"] = store.remaining(check["book_id"])
        check["stop_reason"] = stopped
        store.check(run_id, check)
    complete = all(
        c["coverage"] == "complete" and c["pending_content"] == 0 and not c["errors"]
        for c in checks
    )
    usable = any(c["coverage"] != "failed" for c in checks)
    # Content retry may have produced useful results even if today's list failed.
    usable = usable or any(a["content_status"] == "ready" for a in store.report(run_id)["articles"])
    status = "complete" if complete else "partial" if usable else "failed"
    return store.finish(run_id, status)


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def private_path(variable):
    value = os.environ.get(variable)
    if not value:
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        suffix = {"MPWATCH_DATA_DIR": "data", "MPWATCH_COOKIE_FILE": "cookie.txt"}[variable]
        value = base / "mpwatch" / suffix
    path = Path(value).expanduser().resolve()
    # Recognize this source checkout even when the command runs elsewhere.
    cwd = Path.cwd().resolve()
    roots = {cwd, *cwd.parents}
    module_root = Path(__file__).resolve().parents[2]
    if (module_root / "pyproject.toml").exists():
        roots.add(module_root)
    for root in roots:
        if (root / ".git").exists() and path.is_relative_to(root):
            raise ValueError("private_path_inside_repository")
    return path


def read_cookie():
    path = private_path("MPWATCH_COOKIE_FILE")
    cookie = path.read_text(encoding="utf-8-sig").strip()
    if not cookie or "\n" in cookie or "\r" in cookie or "=" not in cookie:
        raise ValueError("cookie_file_requires_one_header_line")
    return cookie


def make_collector():
    return Collector(
        read_cookie(),
        renew=lambda previous: renew_cookie(private_path("MPWATCH_COOKIE_FILE"), previous),
    )


def parser():
    result = argparse.ArgumentParser(prog="mpwatch")
    commands = result.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="QR login and verify saved authentication")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_login = auth_commands.add_parser("login", help="open an isolated browser for QR login")
    auth_login.add_argument(
        "--browser",
        choices=["msedge", "chrome", "chromium"],
        default="msedge" if os.name == "nt" else "chromium",
    )
    auth_login.add_argument("--timeout", type=positive, default=300)
    auth_commands.add_parser("status", help="verify saved Cookie against WeRead")
    auth_commands.add_parser("renew", help="renew saved authentication without QR login")
    sources = commands.add_parser("sources", help="read WeRead shelf (no remote writes)")
    sources.add_argument("--local", action="store_true", help="list local configuration offline")
    search = commands.add_parser("search", help="search WeRead official accounts by name")
    search.add_argument("keyword")
    sub = commands.add_parser("subscribe", help="add to WeRead shelf and enable local monitoring")
    target = sub.add_mutually_exclusive_group(required=True)
    target.add_argument("--book-id")
    target.add_argument("--name")
    target.add_argument("--article-url")
    sub.add_argument("--dry-run", action="store_true", help="resolve and preview without writing")
    add = commands.add_parser("add", help="add/update local monitored account")
    add.add_argument("--book-id", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--disabled", action="store_true")
    update = commands.add_parser("refresh", help="fetch once and save results")
    update.add_argument("--max-pages", type=positive, default=3)
    update.add_argument("--max-content", type=positive, default=20)
    report = commands.add_parser("report", help="read a saved run offline")
    report.add_argument("--run-id", required=True)
    return result


def main(argv=None):
    # Windows redirected stdout otherwise uses a legacy codepage for Chinese JSON.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    code = 0
    try:
        if args.command == "auth":
            if args.auth_command == "login":
                result = {
                    "schema_version": 1,
                    **login(
                        private_path("MPWATCH_COOKIE_FILE"),
                        browser=args.browser,
                        timeout=args.timeout,
                    ),
                }
            elif args.auth_command == "renew":
                result = {
                    "schema_version": 1,
                    **verify(renew_cookie(private_path("MPWATCH_COOKIE_FILE"))),
                    "renewal": "verified",
                }
            else:
                with closing(make_collector()) as collector:
                    sources = collector.sources()
                    result = {
                        "schema_version": 1,
                        "status": "authenticated",
                        "sources": sources,
                        "source_count": len(sources),
                        "verification": "weread_shelf",
                        "renewal_attempted": collector.renew_attempted,
                    }
        elif args.command == "search":
            if not args.keyword.strip():
                raise ValueError("empty_keyword")
            with closing(make_collector()) as collector:
                result = {
                    "schema_version": 1,
                    "accounts": collector.search_accounts(args.keyword.strip()),
                    "scope": "first_20_search_results",
                }
        elif args.command == "subscribe":
            with closing(make_collector()) as collector:
                account = resolve(
                    collector, book_id=args.book_id, name=args.name, article_url=args.article_url
                )
                if args.dry_run:
                    result = {
                        "schema_version": 1,
                        **subscribe(None, collector, account, dry_run=True),
                    }
                else:
                    with (
                        writer(private_path("MPWATCH_DATA_DIR")),
                        closing(Store(private_path("MPWATCH_DATA_DIR"))) as store,
                    ):
                        result = {"schema_version": 1, **subscribe(store, collector, account)}
        elif args.command == "sources" and not args.local:
            with closing(make_collector()) as collector:
                result = {
                    "schema_version": 1,
                    "sources": collector.sources(),
                    "scope": "weread_shelf_only",
                }
        else:
            root = private_path("MPWATCH_DATA_DIR")
            if args.command in {"report", "sources"}:
                with closing(Store(root, readonly=True)) as store:
                    result = (
                        store.report(args.run_id)
                        if args.command == "report"
                        else {"schema_version": 1, "sources": store.sources()}
                    )
            else:
                with writer(root), closing(Store(root)) as store:
                    if args.command == "add":
                        if not re.fullmatch(r"MP_WXS_[A-Za-z0-9_-]+", args.book_id):
                            raise ValueError("invalid_book_id")
                        if not args.name.strip():
                            raise ValueError("empty_name")
                        store.add(args.book_id, args.name.strip(), not args.disabled)
                        result = {"schema_version": 1, "status": "saved", "book_id": args.book_id}
                    else:
                        with closing(make_collector()) as collector:
                            result = refresh(
                                store,
                                collector,
                                max_pages=args.max_pages,
                                max_content=args.max_content,
                            )
                        code = {"complete": 0, "partial": 2, "failed": 1}[result["status"]]
    except CollectError as error:
        result, code = {"schema_version": 1, "status": "failed", "error": error.as_dict()}, 1
        if error.kind == "reauth_required":
            result["next_action"] = "mpwatch auth login"
    except AuthError as error:
        result, code = {"schema_version": 1, "status": "failed", "error": str(error)}, 1
    except SubscriptionError as error:
        code = 2 if error.details["kind"] in {"subscription_unverified", "local_save_failed"} else 1
        result = {
            "schema_version": 1,
            "status": "partial" if code == 2 else "failed",
            "error": error.details,
        }
    except Timeout:
        result, code = {"schema_version": 1, "status": "failed", "error": "writer_busy"}, 1
    except (OSError, sqlite3.Error, ValueError) as error:
        # Known local validation messages are safe; OS/SQLite messages can expose paths/data.
        message = str(error) if type(error) is ValueError else type(error).__name__
        result, code = {"schema_version": 1, "status": "failed", "error": message}, 1
    except KeyboardInterrupt:
        result, code = {"schema_version": 1, "status": "interrupted"}, 130
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
