from src.tools.dedup import DeduplicationStore


class SeenStore(DeduplicationStore):
    def is_seen(self, url: str) -> bool:
        return self.check_already_seen(url)

    def mark_seen(self, url: str, beat: str = "ricerca_ai_modelli") -> None:
        self.mark_as_seen([url], beat=beat)

    def count(self) -> int:
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM seen_urls").fetchone()[0])
        finally:
            conn.close()

    def close(self) -> None:
        return None
