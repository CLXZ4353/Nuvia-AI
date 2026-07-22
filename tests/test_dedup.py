import tempfile

from src.tools.dedup import DeduplicationStore


def test_dedup_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DeduplicationStore(f"{tmpdir}/test.db")
        url = "https://arxiv.org/abs/2506.12345"

        assert not store.check_already_seen(url)
        store.mark_as_seen([url])
        assert store.check_already_seen(url)


def test_dedup_different_urls():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DeduplicationStore(f"{tmpdir}/test.db")
        store.mark_as_seen(["https://arxiv.org/abs/0001"])
        assert not store.check_already_seen("https://arxiv.org/abs/0002")


def test_dedup_canonicalizes_tracking_parameters_and_trailing_slash():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DeduplicationStore(f"{tmpdir}/test.db")
        store.mark_as_seen(["https://www.arxiv.org/abs/0001/?utm_source=newsletter"])

        assert store.check_already_seen("https://arxiv.org/abs/0001")


def test_source_snapshot_detects_content_changes(tmp_path):
    store = DeduplicationStore(str(tmp_path / "test.db"))
    item = {"source_id": "leaderboard", "_content_hash": "abc"}

    assert not store.source_content_is_unchanged("leaderboard", "abc")
    store.save_source_snapshots([item])
    assert store.source_content_is_unchanged("leaderboard", "abc")
    assert not store.source_content_is_unchanged("leaderboard", "def")
