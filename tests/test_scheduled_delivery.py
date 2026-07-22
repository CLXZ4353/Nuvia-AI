from datetime import datetime, timezone

from src import gui
from src.scheduled_run import scheduled_run_due


def test_weekly_email_delivery_uses_exact_subject_period_and_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "NEWSLETTER_SUBSCRIBERS_PATH", tmp_path / "newsletter_subscribers.json")
    monkeypatch.setattr(gui, "NEWSLETTER_DELIVERY_STATE_PATH", tmp_path / "newsletter_delivery_state.json")
    monkeypatch.setattr(gui, "NEWSLETTER_OUTBOX_DIR", tmp_path / "newsletter_outbox")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_FROM", "digest@example.test")
    gui._write_json_subscribers(
        gui.NEWSLETTER_SUBSCRIBERS_PATH,
        ["first@example.test", "second@example.test"],
    )
    sent = []
    monkeypatch.setattr(gui, "_send_email", lambda message: sent.append(message))

    first = gui.send_weekly_newsletter_to_subscribers(reference_date=datetime(2026, 7, 17).date())
    second = gui.send_weekly_newsletter_to_subscribers(reference_date=datetime(2026, 7, 17).date())

    assert first == {"sent": 2, "failed": 0, "skipped": 0}
    assert second == {"sent": 0, "failed": 0, "skipped": 2}
    assert [message["Subject"] for message in sent] == [gui.NEWSLETTER_SUBJECT, gui.NEWSLETTER_SUBJECT]
    assert "dal 13/07/2026 al 17/07/2026 (inclusi)" in sent[0].get_content()


def test_weekly_telegram_delivery_runs_on_the_same_friday_and_is_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "TELEGRAM_SUBSCRIBERS_PATH", tmp_path / "telegram_subscribers.json")
    monkeypatch.setattr(gui, "TELEGRAM_DELIVERY_STATE_PATH", tmp_path / "telegram_delivery_state.json")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_BROADCAST_DELAY_SECONDS", "0")
    gui._write_json_subscribers(gui.TELEGRAM_SUBSCRIBERS_PATH, ["101", "202"])
    sent = []
    monkeypatch.setattr(gui, "_weekly_telegram_message", lambda reference_date: "Digest di prova")
    monkeypatch.setattr(gui, "_send_telegram_message", lambda chat_id, message: sent.append((chat_id, message)))

    first = gui.send_weekly_telegram_digest_to_subscribers(reference_date=datetime(2026, 7, 17).date())
    second = gui.send_weekly_telegram_digest_to_subscribers(reference_date=datetime(2026, 7, 17).date())
    tuesday = gui.send_weekly_telegram_digest_to_subscribers(reference_date=datetime(2026, 7, 21).date())

    assert first == {"sent": 2, "failed": 0, "skipped": 0}
    assert second == {"sent": 0, "failed": 0, "skipped": 2}
    assert tuesday == {"sent": 0, "failed": 0, "skipped": 0}
    assert sent == [("101", "Digest di prova"), ("202", "Digest di prova")]


def test_github_schedule_keeps_nine_am_in_rome_across_daylight_saving_time():
    assert scheduled_run_due(now=datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc))
    assert not scheduled_run_due(now=datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc))
    assert scheduled_run_due(now=datetime(2026, 1, 21, 8, 0, tzinfo=timezone.utc))
    assert not scheduled_run_due(now=datetime(2026, 1, 21, 7, 0, tzinfo=timezone.utc))
