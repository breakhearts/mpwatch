"""SQLite facts and immutable per-run reports."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from filelock import FileLock


def now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def writer(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / "writer.lock", timeout=0):
        yield


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 book_id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 added_at TEXT NOT NULL, initialized_at TEXT
);
CREATE TABLE IF NOT EXISTS articles (
 article_id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES sources(book_id),
 review_id TEXT NOT NULL, metadata TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 discovery_kind TEXT NOT NULL, content_status TEXT NOT NULL DEFAULT 'pending',
 content_hash TEXT, last_attempt TEXT, next_retry TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 UNIQUE(book_id, review_id)
);
CREATE TABLE IF NOT EXISTS content_versions (
 article_id INTEGER NOT NULL REFERENCES articles(article_id), content_hash TEXT NOT NULL,
 text TEXT NOT NULL, html TEXT NOT NULL, fetched_at TEXT NOT NULL,
 PRIMARY KEY(article_id, content_hash)
);
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL, result TEXT
);
CREATE TABLE IF NOT EXISTS source_checks (
 run_id TEXT NOT NULL REFERENCES runs(run_id), book_id TEXT NOT NULL REFERENCES sources(book_id),
 snapshot TEXT NOT NULL, PRIMARY KEY(run_id, book_id)
);
CREATE TABLE IF NOT EXISTS check_articles (
 run_id TEXT NOT NULL REFERENCES runs(run_id), article_id INTEGER NOT NULL REFERENCES articles(article_id),
 snapshot TEXT NOT NULL, PRIMARY KEY(run_id, article_id)
);
CREATE INDEX IF NOT EXISTS content_queue ON articles(content_status, next_retry, last_attempt);
"""


class Store:
    def __init__(self, root, *, readonly=False):
        path = Path(root) / "mpwatch.sqlite3"
        if readonly:
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1) or (readonly and version != 1):
            self.db.close()
            raise ValueError("unsupported_database_version")
        if not readonly:
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA user_version=1")

    def close(self):
        self.db.close()

    def add(self, book_id, name, enabled=True):
        with self.db:
            self.db.execute(
                """INSERT INTO sources(book_id,name,enabled,added_at) VALUES(?,?,?,?)
                ON CONFLICT(book_id) DO UPDATE SET name=excluded.name,enabled=excluded.enabled""",
                (book_id, name, int(enabled), now()),
            )

    def sources(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM sources ORDER BY book_id")]

    def start(self):
        # The caller owns the OS writer lock before recovery.
        old = self.db.execute("SELECT run_id FROM runs WHERE status='running'").fetchall()
        for row in old:
            self.finish(row[0], "interrupted")
        run_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO runs VALUES(?,?,NULL,'running',NULL)", (run_id, now()))
        return run_id

    def _active(self, run_id):
        row = self.db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row[0] != "running":
            raise ValueError("run_is_not_active")

    def check(self, run_id, check):
        with self.db:
            self._active(run_id)
            self.db.execute(
                "INSERT OR REPLACE INTO source_checks VALUES(?,?,?)",
                (run_id, check["book_id"], encode(check)),
            )

    def initialize(self, book_id):
        with self.db:
            self.db.execute(
                "UPDATE sources SET initialized_at=COALESCE(initialized_at,?) WHERE book_id=?",
                (now(), book_id),
            )

    def discover(self, run_id, source, article):
        with self.db:
            self._active(run_id)
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO articles
                (book_id,review_id,metadata,first_seen_at,discovery_kind) VALUES(?,?,?,?,?)""",
                (
                    source["book_id"],
                    article["review_id"],
                    encode(article),
                    now(),
                    "observed" if source["initialized_at"] else "baseline",
                ),
            )
            if cursor.rowcount:
                article_id = cursor.lastrowid
                event = "discovered" if source["initialized_at"] else "baseline"
                self._snapshot(run_id, article_id, event)
                return True
            self.db.execute(
                "UPDATE articles SET metadata=? WHERE book_id=? AND review_id=?",
                (encode(article), source["book_id"], article["review_id"]),
            )
            return False

    def _snapshot(self, run_id, article_id, event, error=None):
        row = dict(
            self.db.execute("SELECT * FROM articles WHERE article_id=?", (article_id,)).fetchone()
        )
        previous = self.db.execute(
            "SELECT snapshot FROM check_articles WHERE run_id=? AND article_id=?",
            (run_id, article_id),
        ).fetchone()
        if previous:
            old_event = json.loads(previous[0])["event_kind"]
            if old_event in {"baseline", "discovered"}:
                event = old_event
        snapshot = {
            **json.loads(row["metadata"]),
            "article_id": article_id,
            "book_id": row["book_id"],
            "first_seen_at": row["first_seen_at"],
            "discovery_kind": row["discovery_kind"],
            "event_kind": event,
            "content_status": row["content_status"],
            "content_hash": row["content_hash"],
            "error": error,
            "body": None,
        }
        if row["content_hash"]:
            version = self.db.execute(
                "SELECT text,fetched_at FROM content_versions "
                "WHERE article_id=? AND content_hash=?",
                (article_id, row["content_hash"]),
            ).fetchone()
            snapshot["body"] = dict(version)
        self.db.execute(
            "INSERT OR REPLACE INTO check_articles VALUES(?,?,?)",
            (run_id, article_id, encode(snapshot)),
        )

    def pending(self, limit):
        return [
            dict(row)
            for row in self.db.execute(
                """SELECT a.* FROM articles a
            JOIN sources s USING(book_id) WHERE s.enabled=1 AND a.content_status!='ready'
            AND (a.next_retry IS NULL OR a.next_retry<=?)
            ORDER BY a.last_attempt IS NOT NULL,a.last_attempt,a.first_seen_at,a.article_id LIMIT ?""",
                (now(), limit),
            )
        ]

    def remaining(self, book_id):
        return self.db.execute(
            "SELECT COUNT(*) FROM articles WHERE book_id=? AND content_status!='ready'", (book_id,)
        ).fetchone()[0]

    def body(self, run_id, article_id, content=None, error=None):
        with self.db:
            self._active(run_id)
            stamp = now()
            if content is not None:
                digest = hashlib.sha256(encode(content).encode("utf-8")).hexdigest()
                self.db.execute(
                    "INSERT OR IGNORE INTO content_versions VALUES(?,?,?,?,?)",
                    (article_id, digest, content["text"], content["html"], stamp),
                )
                self.db.execute(
                    "UPDATE articles SET content_status='ready',content_hash=?,"
                    "last_attempt=?,next_retry=NULL,attempts=attempts+1 WHERE article_id=?",
                    (digest, stamp, article_id),
                )
                self._snapshot(run_id, article_id, "content_ready")
            else:
                attempts = self.db.execute(
                    "SELECT attempts FROM articles WHERE article_id=?", (article_id,)
                ).fetchone()[0]
                retry = (
                    datetime.fromisoformat(stamp)
                    + timedelta(seconds=min(3600, 30 * 2 ** min(attempts, 7)))
                ).isoformat(timespec="microseconds")
                self.db.execute(
                    "UPDATE articles SET content_status='failed',last_attempt=?,"
                    "next_retry=?,attempts=attempts+1 WHERE article_id=?",
                    (stamp, retry, article_id),
                )
                self._snapshot(run_id, article_id, "content_failed", error)

    def _result(self, run_id):
        run = dict(
            self.db.execute(
                "SELECT run_id,started_at,finished_at,status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        )
        checks = [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT snapshot FROM source_checks WHERE run_id=? ORDER BY book_id", (run_id,)
            )
        ]
        articles = [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT snapshot FROM check_articles WHERE run_id=? ORDER BY article_id", (run_id,)
            )
        ]
        return {"schema_version": 1, **run, "checks": checks, "articles": articles}

    def finish(self, run_id, status):
        with self.db:
            self._active(run_id)
            self.db.execute(
                "UPDATE runs SET status=?,finished_at=? WHERE run_id=?", (status, now(), run_id)
            )
            result = self._result(run_id)
            self.db.execute("UPDATE runs SET result=? WHERE run_id=?", (encode(result), run_id))
        return result

    def report(self, run_id):
        row = self.db.execute("SELECT result FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("run_not_found")
        return json.loads(row[0]) if row[0] else self._result(run_id)
