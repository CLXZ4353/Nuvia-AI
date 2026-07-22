import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from src.tools.urls import canonicalize_url


class DeduplicationStore:
    def __init__(self, db_path: str, retention_days: int = 90):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_urls (
                    url TEXT PRIMARY KEY,
                    seen_at TEXT NOT NULL,
                    beat TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_snapshots (
                    source_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def check_already_seen(self, url: str) -> bool:
        canonical_url = canonicalize_url(url)
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT 1 FROM seen_urls WHERE url IN (?, ?)",
                (canonical_url, url),
            )
            try:
                return cursor.fetchone() is not None
            finally:
                cursor.close()
        finally:
            conn.close()

    def mark_as_seen(self, urls: list[str], beat: str = "ricerca_ai_modelli") -> None:
        now = datetime.utcnow().isoformat()
        canonical_urls = list(dict.fromkeys(canonicalize_url(url) for url in urls))
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.executemany(
                "INSERT OR IGNORE INTO seen_urls (url, seen_at, beat) VALUES (?, ?, ?)",
                [(url, now, beat) for url in canonical_urls],
            )
            try:
                conn.commit()
            finally:
                cursor.close()
        finally:
            conn.close()

    def cleanup_old(self) -> None:
        cutoff = (datetime.utcnow() - timedelta(days=self.retention_days)).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute("DELETE FROM seen_urls WHERE seen_at < ?", (cutoff,))
            try:
                conn.commit()
            finally:
                cursor.close()
        finally:
            conn.close()

    def source_content_is_unchanged(self, source_id: str, content_hash: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT content_hash FROM source_snapshots WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            return row is not None and row[0] == content_hash
        finally:
            conn.close()

    def save_source_snapshots(self, items: list[dict]) -> None:
        now = datetime.utcnow().isoformat()
        snapshots = {
            item["source_id"]: item["_content_hash"]
            for item in items
            if item.get("source_id") and item.get("_content_hash")
        }
        if not snapshots:
            return
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                """
                INSERT INTO source_snapshots (source_id, content_hash, checked_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    checked_at = excluded.checked_at
                """,
                [(source_id, content_hash, now) for source_id, content_hash in snapshots.items()],
            )
            conn.commit()
        finally:
            conn.close()
