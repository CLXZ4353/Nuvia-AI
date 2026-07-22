from datetime import date, timedelta
import json
import logging
import os

import pytest

from src import auth, gui
from src import notifications
from src.gui import (
    DigestHistoryItem,
    DigestView,
    TelegramDeliveryError,
    clear_notification_suspension,
    configure_notification_suspension,
    digest_history,
    load_gui_env,
    parse_digest_markdown,
    process_telegram_start_updates,
    render_html,
    render_settings_html,
    send_weekly_telegram_digest_to_subscribers,
    subscribe_and_send_newsletter,
    _telegram_chunks,
    _telegram_digest_text,
    _telegram_link_pair,
    _send_telegram_message,
    _weekly_telegram_message,
    _weekly_newsletter_message,
    _title_key_point,
    _smtp_username,
)


def test_parse_digest_markdown_extracts_entries():
    markdown = """# AI Research Digest - 25/06/2026
*Beat: Ricerca AI / modelli - 1 voci*

## 1. Test title
**Fonte:** Example - **Data:** 2026-06-25 - **Categoria:** agenti - **Rilevanza:** 4/5
**URL:** https://example.com/item

**Sintesi:** Prima frase. Seconda frase.

**Perche conta:** Impatto pratico.

---

*Generato il oggi - Fonti consultate: 1*
"""

    view = parse_digest_markdown(markdown)

    assert view.title == "AI Research Digest - 25/06/2026"
    assert len(view.entries) == 1
    assert view.entries[0]["url"] == "https://example.com/item"
    assert view.entries[0]["summary"] == "Prima frase. Seconda frase."


def test_render_html_contains_digest_content():
    view = parse_digest_markdown(
        """# Digest
*Beat: Test*

## 1. Titolo
**Fonte:** Fonte - **Data:** 2026-06-25 - **Categoria:** agenti - **Rilevanza:** 4/5
**URL:** https://example.com

**Sintesi:** Prima frase. Seconda frase.

**Perche conta:** Aiuta il team.
"""
    )

    page = render_html(view)

    assert "<!doctype html>" in page
    assert "Titolo" in page
    assert "https://example.com" in page
    assert 'href="/today"' in page
    assert 'href="/settings"' in page
    assert "&#9881;" in page
    assert "<strong>Perché conta:</strong>" in page


def test_legacy_card_and_api_titles_are_plain_and_limited():
    entry = {
        "title": "Model-X: l'évolution des modèles! 🤖 / release [beta]",
        "meta": "**Fonte:** Example - **Data:** 2026-06-25",
        "url": "https://example.com",
        "summary": "Prima frase. Seconda frase.",
        "why": "Aiuta il team.",
    }

    card = gui._render_entry(entry)
    api_record = gui._api_news_record(entry)

    assert "Model X l evolution des modeles release beta" in card
    assert api_record["title"] == "Model X l evolution des modeles release beta"
    assert len(api_record["title"]) <= 80


def test_render_settings_html_contains_newsletter_form():
    page = render_settings_html()

    assert "Impostazioni newsletter" in page
    assert '<a class="back" href="/" aria-label="Torna alla pagina principale">&larr; Indietro</a>' in page
    assert 'method="post" action="/newsletter"' in page
    assert 'method="post" action="/telegram"' not in page
    assert 'name="chat_id"' not in page
    assert 'type="email"' in page
    assert "Invia newsletter" in page
    assert "Telegram non configurato" in page
    assert "Apri nel browser" not in page
    assert "Collega nell’app Telegram" not in page
    assert "Copia comando" not in page
    assert "Ho scritto al bot, inviami il digest" not in page
    assert "digest della settimana corrente verr" in page
    assert "Notifiche push" in page
    assert 'id="push-enable"' in page
    assert "/api/firebase/config" in page
    assert "Sospensione notifiche" in page
    assert 'action="/notifications/suspension"' in page
    assert 'data-date-picker' in page
    assert "Anteprima ultimo digest" not in page
    assert "ultimo digest disponibile" not in page
    assert "Digest selezionato" not in page


def test_subscribe_and_send_newsletter_saves_outbox_without_smtp(tmp_path, monkeypatch):
    subscribers_path = tmp_path / "newsletter_subscribers.json"
    outbox_dir = tmp_path / "newsletter_outbox"
    monkeypatch.setattr("src.gui.NEWSLETTER_SUBSCRIBERS_PATH", subscribers_path)
    monkeypatch.setattr("src.gui.NEWSLETTER_OUTBOX_DIR", outbox_dir)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)

    result = subscribe_and_send_newsletter("Utente@Example.com")

    assert result.ok
    assert not result.delivered
    assert "utente@example.com" in subscribers_path.read_text(encoding="utf-8")
    assert list(outbox_dir.glob("*.eml"))


def test_subscribe_and_send_newsletter_rejects_invalid_email(tmp_path, monkeypatch):
    monkeypatch.setattr("src.gui.NEWSLETTER_SUBSCRIBERS_PATH", tmp_path / "subscribers.json")
    monkeypatch.setattr("src.gui.NEWSLETTER_OUTBOX_DIR", tmp_path / "outbox")

    result = subscribe_and_send_newsletter("non-valida")

    assert not result.ok
    assert not (tmp_path / "subscribers.json").exists()


def test_telegram_chunks_respect_message_limit():
    text = ("titolo\n\n" + ("x" * 1000) + "\n\n") * 5

    chunks = _telegram_chunks(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= 4096 for chunk in chunks)


def test_telegram_digest_text_does_not_duplicate_subject_when_body_has_title():
    subject = "Research Digest AI - settimana 06/07 - 07/07/2026"
    body = f"{subject}\n\nBeat: Test\n\n1. Prima notizia"

    text = _telegram_digest_text(subject, body)

    assert text.count(subject) == 1
    assert text.startswith(subject)


def test_send_telegram_message_uses_html_parse_mode(monkeypatch):
    sent_payloads = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, json):
            sent_payloads.append(json)
            return FakeResponse()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr("src.gui.httpx.Client", FakeClient)

    _send_telegram_message("123456789", "Test <b>bold</b>")

    assert sent_payloads[0]["parse_mode"] == "HTML"
    assert sent_payloads[0]["text"] == "Test <b>bold</b>"


def test_telegram_send_logs_request_and_response_without_token(monkeypatch, caplog):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"message_id": 77, "chat": {"id": 123456789}}}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, json):
            assert "secret-token" in url
            return FakeResponse()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setattr("src.gui.httpx.Client", FakeClient)
    caplog.set_level(logging.INFO, logger="src.gui")

    _send_telegram_message("123456789", "Digest verificato")

    assert "sendMessage request" in caplog.text
    assert "sendMessage response" in caplog.text
    assert "secret-token" not in caplog.text


def test_weekly_message_uses_channel_specific_footer():
    _, email_body = _weekly_newsletter_message(channel="email")
    _, telegram_body = _weekly_newsletter_message(channel="telegram")

    assert "questa email" in email_body
    assert "questa mail" not in telegram_body
    assert "questo messaggio" in telegram_body
    assert "bot Telegram" in telegram_body


def test_weekly_telegram_message_uses_structured_digest_template(monkeypatch):
    summary = "Sintesi completa della notizia di test. Questa frase deve restare identica nel messaggio Telegram."
    why = "Perché conta completo della notizia di test, senza tagli e senza riscritture."
    digest_view = DigestView(
        path=None,
        title="AI Research Digest - 07/07/2026",
        subtitle="Ricerca AI / modelli - 1 voci",
        warning=None,
        entries=[
            {
                "title": "Titolo dinamico della notizia aggiornata",
                "meta": "Fonte: Fonte Test - Data: 2026-07-06 - Categoria: benchmark - Rilevanza: 4/5",
                "url": "https://example.com/notizia-aggiornata",
                "summary": summary,
                "why": why,
            }
        ],
        footer=[],
        digest_date="2026-07-07",
    )
    monkeypatch.setattr("src.gui._current_week_digest_view", lambda reference_date=None: digest_view)

    text = _weekly_telegram_message(date(2026, 7, 7))

    assert "🤖 <b>Research Digest AI</b>" in text
    assert "<b>Settimana 06/07 - 07/07/2026</b>" in text
    assert "📚 Ricerca AI / Modelli (1 voce)" in text
    assert "1️⃣ <b>Titolo dinamico della notizia aggiornata</b>" in text
    assert "📅 <b>Data:</b> 2026-07-06" in text
    assert "📖 <b>Fonte:</b> Fonte Test" in text
    assert "🏷️ <b>Categoria:</b> benchmark" in text
    assert "⭐ <b>Rilevanza:</b> 4/5" in text
    assert summary in text
    assert "💡 <b>Perché conta:</b>" in text
    assert why in text
    assert "..." not in text
    assert "🔗 https://example.com/notizia-aggiornata" in text
    assert text.endswith("Ricevi questo messaggio perché hai richiesto il Research Digest AI dal sito del Research Digest Agent.")


def test_telegram_link_pair_uses_configured_username(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "nuvia_news_bot")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")

    assert _telegram_link_pair("connect_abc") == (
        "tg://resolve?domain=nuvia_news_bot&start=connect_abc",
        "https://t.me/nuvia_news_bot?start=connect_abc",
    )


def test_gmail_smtp_username_falls_back_to_from_when_username_is_not_email(monkeypatch):
    monkeypatch.setenv("SMTP_FROM", "sender@gmail.com")
    monkeypatch.setenv("SMTP_USERNAME", "Display Name")

    assert _smtp_username("smtp.gmail.com") == "sender@gmail.com"


def test_load_gui_env_reads_project_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    (tmp_path / ".env").write_text("SMTP_HOST=smtp.gmail.com\n", encoding="utf-8")

    load_gui_env()

    assert __import__("os").environ["SMTP_HOST"] == "smtp.gmail.com"


def test_render_html_uses_entry_box_when_digest_has_no_news():
    view = parse_digest_markdown(
        """# Digest corrente
*Beat: Test - 0 voci*
"""
    )

    page = render_html(view, history=[])

    assert "<section class=\"grid\"><article>" in page
    assert "Nessuna voce disponibile." in page
    assert "oggi non nuove notizie" in page
    assert "Aggiornamento settimanale" in page
    assert "<strong>Perché conta:</strong>" in page


def test_render_html_contains_history_sidebar_navigation():
    view = parse_digest_markdown(
        """# Digest corrente
*Beat: Test*
""",
        source_path=None,
    )
    history = [
        DigestHistoryItem(
            id="data/digests/digest_2026-07-01_165702.md",
            path="data/digests/digest_2026-07-01_165702.md",
            title="AI Research Digest - 01/07/2026",
            subtitle="Beat: Test",
            updated="01/07/2026 16:57",
            key_points=["Primo punto chiave", "Secondo punto chiave"],
            entry_count=3,
            digest_date="2026-07-01",
        )
    ]

    page = render_html(
        view,
        history,
        current_path="/?sidebar=open",
        show_history_sidebar=True,
    )

    assert 'aria-label="Apri cronologia"' in page
    assert 'data-sidebar-target="/?sidebar=open"' in page
    assert 'class="app-sidebar-link active"' not in page
    assert 'aria-current="page"' not in page
    assert 'class="sidebar-open"' in page
    assert "history-page-hint" not in page
    assert "history-toggle" not in page
    assert 'class="history-panel"' in page
    assert 'id="history-search"' in page
    assert page.index('>Cronologia</span>') < page.index('id="history-search"')
    assert page.index('id="history-search"') < page.index('data-history-item')
    assert '<details class="history-item"' in page
    accordion_start = page.index('<details class="history-item"')
    accordion_end = page.index("</details>", accordion_start)
    accordion_body = page[page.index('<div class="history-body">', accordion_start):accordion_end]
    assert "Primo punto chiave" not in accordion_body
    assert (
        'href="/digest?date=2026-07-01" target="_blank" '
        'rel="noopener noreferrer">Leggi di pi&ugrave;'
    ) in accordion_body
    main = page[page.index('<main id="canvas-container">') : page.index("</main>")]
    assert '<section class="grid">' in main


def test_digest_history_key_points_summarize_entry_titles(tmp_path):
    digest_dir = tmp_path / "digests"
    digest_dir.mkdir()
    digest_path = digest_dir / "digest.md"
    digest_path.write_text(
        """# Digest
*Beat: Test*

## 1. Primo titolo molto importante
**Fonte:** Fonte A
**URL:** https://example.com/a

**Sintesi:** Sintesi che non deve comparire nella preview.

## 2. Secondo titolo
**Fonte:** Fonte B
**URL:** https://example.com/b

**Sintesi:** Altra sintesi.
""",
        encoding="utf-8",
    )

    history = digest_history((str(digest_dir),))

    assert len(history) == 1
    assert history[0].key_points == ["Primo titolo molto importante", "Secondo titolo"]
    assert all("Sintesi" not in point for point in history[0].key_points)


def test_digest_history_groups_same_day_files_and_keeps_exact_titles(tmp_path):
    digest_dir = tmp_path / "digests"
    digest_dir.mkdir()
    first = digest_dir / "digest_2026-07-02_100000.md"
    second = digest_dir / "digest_2026-07-02_110000.md"
    first.write_text(
        """# AI Research Digest - 02/07/2026
*Beat: Test - 1 voci*

## 1. Introducing Exact Title
**Fonte:** Fonte A
**URL:** https://example.com/a
""",
        encoding="utf-8",
    )
    second.write_text(
        """# AI Research Digest - 02/07/2026
*Beat: Test - 1 voci*

## 1. Second Exact Title Learn more
**Fonte:** Fonte B
**URL:** https://example.com/b
""",
        encoding="utf-8",
    )

    history = digest_history((str(digest_dir),))

    assert len(history) == 1
    assert history[0].id == "date:2026-07-02"
    assert history[0].entry_count == 2
    assert history[0].key_points == ["Introducing Exact Title", "Second Exact Title Learn more"]


def test_digest_history_uses_generated_footer_timestamp_instead_of_mtime(tmp_path):
    digest_dir = tmp_path / "digests"
    digest_dir.mkdir()
    first = digest_dir / "digest_2026-07-01.md"
    second = digest_dir / "digest_2026-07-02.md"
    first.write_text(
        """# AI Research Digest - 01/07/2026
*Beat: Test - 0 voci*

*Generato il 2026-07-01T08:15:00 - Fonti consultate: 1*
""",
        encoding="utf-8",
    )
    second.write_text(
        """# AI Research Digest - 02/07/2026
*Beat: Test - 0 voci*

*Generato il 2026-07-02T09:30:00 - Fonti consultate: 1*
""",
        encoding="utf-8",
    )
    same_mtime = 1_800_000_000
    os.utime(first, (same_mtime, same_mtime))
    os.utime(second, (same_mtime, same_mtime))

    history = digest_history((str(digest_dir),))

    by_date = {item.digest_date: item for item in history}
    assert by_date["2026-07-01"].updated == "01/07/2026 08:15"
    assert by_date["2026-07-02"].updated == "02/07/2026 09:30"


def test_home_digest_aggregates_only_the_latest_published_week(tmp_path, monkeypatch):
    digest_dir = tmp_path / "digests"
    digest_dir.mkdir()

    for digest_date, title in (
        ("2026-07-09", "Notizia della settimana precedente"),
        ("2026-07-14", "Prima notizia della settimana corrente"),
        ("2026-07-16", "Seconda notizia della settimana corrente"),
    ):
        (digest_dir / f"digest_{digest_date}.md").write_text(
            f"""# AI Research Digest - {digest_date[8:10]}/{digest_date[5:7]}/{digest_date[:4]}
*Beat: Test - 1 voce*

## 1. {title}
**Fonte:** Fonte Test - **Data:** {digest_date} - **Categoria:** test
**URL:** https://example.com/{digest_date}
""",
            encoding="utf-8",
        )

    digest_dirs = (str(digest_dir),)
    real_digest_history = gui.digest_history
    real_load_digest_by_date = gui.load_digest_by_date
    monkeypatch.setattr(gui, "digest_history", lambda: real_digest_history(digest_dirs))
    monkeypatch.setattr(gui, "load_digest_by_date", lambda value: real_load_digest_by_date(value, digest_dirs))
    current = gui.load_current_digest()

    assert current.digest_date == "2026-07-16"
    assert [entry["title"] for entry in current.entries] == [
        "Prima notizia della settimana corrente",
        "Seconda notizia della settimana corrente",
    ]


def test_render_html_hides_json_link_from_toolbar():
    view = parse_digest_markdown(
        """# AI Research Digest - 01/07/2026
*Beat: Test*
""",
    )

    page = render_html(view, history=[])

    assert ">JSON</a>" not in page
    assert 'href="/api/latest?date=2026-07-01"' not in page


def test_render_html_removes_digest_label_from_entry_boxes():
    view = parse_digest_markdown(
        """# AI Research Digest - 01/07/2026
*Beat: Test*

## 1. Titolo
**Fonte:** Fonte - **Data:** 2026-06-25 - **Categoria:** agenti - **Rilevanza:** 4/5
**URL:** https://example.com

**Sintesi:** Prima frase.

**Perche conta:** Aiuta il team.
""",
    )

    page = render_html(view, history=[])

    assert '<span class="label">Digest</span>' not in page
    assert 'class="label label-placeholder"' in page


def test_title_key_point_truncates_at_word_boundary():
    point = _title_key_point("Alpha beta gamma delta epsilon zeta eta theta", limit=24)

    assert point == "Alpha beta gamma..."


def test_render_html_displays_category_without_underscore():
    view = parse_digest_markdown(
        """# Digest
*Beat: Test*

## 1. Titolo
**Fonte:** Fonte - **Data:** 2026-06-25 - **Categoria:** paper_ricerca - **Rilevanza:** 4/5
**URL:** https://example.com

**Sintesi:** Prima frase. Seconda frase.

**Perché conta:** Aiuta il team.
"""
    )

    page = render_html(view, history=[])

    assert "Categoria: paper ricerca" in page
    assert "paper_ricerca" not in page


def test_parse_digest_markdown_cleans_unicode_label_links_and_footer():
    markdown = """# AI Research Digest - 25/06/2026
*Beat: Ricerca AI / modelli - 1 voci*

## 1. Titolo
**Fonte:** Fonte - **Data:** 2026-06-25 - **Categoria:** agenti - **Rilevanza:** 4/5
**URL:** [https://example.com/item](https://example.com/item)

**Sintesi:** Prima frase. Seconda frase.

**Perché conta:** ** Impatto pratico.

---

*Generato il oggi - Fonti consultate: 1*
"""

    view = parse_digest_markdown(markdown)

    assert view.entries[0]["url"] == "https://example.com/item"
    assert view.entries[0]["why"] == "Impatto pratico."
    assert "Generato il" not in view.entries[0]["why"]
    assert view.footer == ["Generato il oggi - Fonti consultate: 1"]


def test_configure_notification_suspension_persists_and_blocks_past_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_SUSPENSION_PATH", tmp_path / "suspension.json")
    today = date.today()

    result = configure_notification_suspension(today.isoformat(), (today + timedelta(days=3)).isoformat())

    assert result.ok
    assert notifications.notifications_suspended()
    assert (tmp_path / "suspension.json").exists()

    invalid = configure_notification_suspension(
        (today - timedelta(days=1)).isoformat(),
        (today + timedelta(days=1)).isoformat(),
    )

    assert not invalid.ok


def test_clear_notification_suspension_removes_persistent_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_SUSPENSION_PATH", tmp_path / "suspension.json")
    today = date.today()
    configure_notification_suspension(today.isoformat(), (today + timedelta(days=1)).isoformat())

    result = clear_notification_suspension()

    assert result.ok
    assert not notifications.notifications_suspended()
    assert not (tmp_path / "suspension.json").exists()


def test_subscribe_newsletter_does_not_create_outbox_when_notifications_are_suspended(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_SUSPENSION_PATH", tmp_path / "suspension.json")
    monkeypatch.setattr("src.gui.NEWSLETTER_SUBSCRIBERS_PATH", tmp_path / "newsletter_subscribers.json")
    monkeypatch.setattr("src.gui.NEWSLETTER_OUTBOX_DIR", tmp_path / "newsletter_outbox")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    today = date.today()
    configure_notification_suspension(today.isoformat(), (today + timedelta(days=1)).isoformat())

    result = subscribe_and_send_newsletter("utente@example.com")

    assert result.ok
    assert not result.delivered
    assert "utente@example.com" in (tmp_path / "newsletter_subscribers.json").read_text(encoding="utf-8")
    assert not (tmp_path / "newsletter_outbox").exists()


def test_process_telegram_start_updates_resends_digest_after_a_private_start(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_SUSPENSION_PATH", tmp_path / "suspension.json")
    monkeypatch.setattr("src.gui.TELEGRAM_SUBSCRIBERS_PATH", tmp_path / "telegram_subscribers.json")
    monkeypatch.setattr("src.gui.TELEGRAM_START_STATE_PATH", tmp_path / "telegram_start_state.json")
    monkeypatch.setattr("src.gui.TELEGRAM_CONTACTS_PATH", tmp_path / "telegram_contacts.json")
    sent = []
    monkeypatch.setattr("src.gui._send_telegram_message", lambda chat_id, text, **_: sent.append((chat_id, text)))
    monkeypatch.setattr("src.gui._initial_telegram_message", lambda: "Digest iniziale")

    first = [
        {
            "update_id": 10,
            "message": {
                "text": "/start",
                "chat": {"id": 123456789, "type": "private"},
                "from": {"id": 123456789, "username": "ada", "first_name": "Ada"},
            },
        }
    ]
    second = [
        {
            "update_id": 11,
            "message": {
                "text": "/start",
                "chat": {"id": 123456789, "type": "private"},
                "from": {"id": 123456789, "username": "ada", "first_name": "Ada"},
            },
        }
    ]

    first_result = process_telegram_start_updates(first)
    second_result = process_telegram_start_updates(second)

    assert first_result[0].delivered
    assert second_result[0].delivered
    assert "123456789" in (tmp_path / "telegram_subscribers.json").read_text(encoding="utf-8")
    assert sent == [
        ("123456789", "Digest iniziale"),
        ("123456789", "Digest iniziale"),
    ]
    state = json.loads((tmp_path / "telegram_start_state.json").read_text(encoding="utf-8"))
    assert state["initial_digest_chat_ids"] == ["123456789"]
    assert state["pending_initial_deliveries"] == []
    contacts = json.loads((tmp_path / "telegram_contacts.json").read_text(encoding="utf-8"))
    assert contacts["123456789"]["username"] == "ada"


def test_process_telegram_start_updates_retries_a_pending_initial_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_SUSPENSION_PATH", tmp_path / "suspension.json")
    monkeypatch.setattr("src.gui.TELEGRAM_SUBSCRIBERS_PATH", tmp_path / "telegram_subscribers.json")
    monkeypatch.setattr("src.gui.TELEGRAM_START_STATE_PATH", tmp_path / "telegram_start_state.json")
    monkeypatch.setattr("src.gui.TELEGRAM_CONTACTS_PATH", tmp_path / "telegram_contacts.json")
    monkeypatch.setattr("src.gui._initial_telegram_message", lambda: "Digest iniziale")
    calls = []

    def send(chat_id, text, *, start_chunk=0):
        calls.append((chat_id, text, start_chunk))
        if len(calls) == 1:
            raise TelegramDeliveryError("temporarily unavailable", retryable=True, sent_chunks=1)
        return 1

    monkeypatch.setattr("src.gui._send_telegram_message", send)
    update = [
        {
            "update_id": 20,
            "message": {
                "text": "/start",
                "chat": {"id": 123456789, "type": "private"},
                "from": {"id": 123456789, "first_name": "Ada"},
            },
        }
    ]

    first_result = process_telegram_start_updates(update)
    first_state = json.loads((tmp_path / "telegram_start_state.json").read_text(encoding="utf-8"))
    retry_result = process_telegram_start_updates([])
    final_state = json.loads((tmp_path / "telegram_start_state.json").read_text(encoding="utf-8"))

    assert not first_result[0].ok
    assert first_state["pending_initial_deliveries"][0]["chat_id"] == "123456789"
    assert retry_result[0].delivered
    assert final_state["initial_digest_chat_ids"] == ["123456789"]
    assert final_state["pending_initial_deliveries"] == []
    assert calls == [
        ("123456789", "Digest iniziale", 0),
        ("123456789", "Digest iniziale", 1),
    ]


def test_account_suspension_blocks_initial_telegram_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_STORE_PATH", tmp_path / "auth_store.json")
    monkeypatch.setattr(notifications, "NOTIFICATION_SUSPENSION_PATH", tmp_path / "global_suspension.json")
    monkeypatch.setattr(notifications, "USER_NOTIFICATION_SUSPENSIONS_PATH", tmp_path / "user_suspensions.json")
    monkeypatch.setattr("src.gui.TELEGRAM_SUBSCRIBERS_PATH", tmp_path / "telegram_subscribers.json")
    monkeypatch.setattr("src.gui.TELEGRAM_START_STATE_PATH", tmp_path / "telegram_start_state.json")
    monkeypatch.setattr("src.gui.TELEGRAM_CONTACTS_PATH", tmp_path / "telegram_contacts.json")
    monkeypatch.setattr("src.gui._initial_telegram_message", lambda: "Digest iniziale")
    sent = []
    monkeypatch.setattr("src.gui._send_telegram_message", lambda *args, **kwargs: sent.append((args, kwargs)))
    registration = auth.register_user("Ada", "Lovelace", "ada@example.com", "SecurePass1!", "SecurePass1!")
    assert registration.ok and registration.user
    user = registration.user
    payload = auth.telegram_connect_payload(user["id"])
    assert payload
    today = gui._project_today()
    notifications.save_user_notification_suspension(user["id"], today, today + timedelta(days=1))

    result = process_telegram_start_updates(
        [
            {
                "update_id": 21,
                "message": {
                    "text": f"/start {payload}",
                    "chat": {"id": 123456789, "type": "private"},
                    "from": {"id": 123456789, "first_name": "Ada"},
                },
            }
        ]
    )

    assert result[0].ok
    assert result[0].title == "Invio Telegram sospeso"
    assert sent == []
    assert auth.telegram_chat_id_for_user(user["id"]) == "123456789"


def test_legacy_initial_delivery_flag_does_not_block_first_receipted_start(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_SUSPENSION_PATH", tmp_path / "suspension.json")
    monkeypatch.setattr("src.gui.TELEGRAM_SUBSCRIBERS_PATH", tmp_path / "telegram_subscribers.json")
    monkeypatch.setattr("src.gui.TELEGRAM_START_STATE_PATH", tmp_path / "telegram_start_state.json")
    monkeypatch.setattr("src.gui.TELEGRAM_CONTACTS_PATH", tmp_path / "telegram_contacts.json")
    (tmp_path / "telegram_start_state.json").write_text(
        json.dumps({"last_update_id": 5, "initial_digest_chat_ids": ["123456789"]}),
        encoding="utf-8",
    )
    sent = []
    monkeypatch.setattr("src.gui._initial_telegram_message", lambda: "Digest corrente")
    monkeypatch.setattr(
        "src.gui._send_telegram_message",
        lambda chat_id, text, **_: sent.append((chat_id, text)),
    )

    result = process_telegram_start_updates(
        [
            {
                "update_id": 6,
                "message": {
                    "text": "/start",
                    "chat": {"id": 123456789, "type": "private"},
                    "from": {"id": 123456789, "first_name": "Ada"},
                },
            }
        ]
    )
    state = json.loads((tmp_path / "telegram_start_state.json").read_text(encoding="utf-8"))

    assert result[0].delivered
    assert sent == [("123456789", "Digest corrente")]
    assert state["initial_delivery_receipts"]["123456789"]["week"]
    assert state["initial_delivery_receipts"]["123456789"]["message_length"] == len("Digest corrente")


def test_process_telegram_start_updates_rejects_groups_and_sender_mismatches(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_SUSPENSION_PATH", tmp_path / "suspension.json")
    monkeypatch.setattr("src.gui.TELEGRAM_SUBSCRIBERS_PATH", tmp_path / "telegram_subscribers.json")
    monkeypatch.setattr("src.gui.TELEGRAM_START_STATE_PATH", tmp_path / "telegram_start_state.json")
    monkeypatch.setattr("src.gui.TELEGRAM_CONTACTS_PATH", tmp_path / "telegram_contacts.json")
    sent = []
    monkeypatch.setattr("src.gui._send_telegram_message", lambda *args, **kwargs: sent.append((args, kwargs)))

    results = process_telegram_start_updates(
        [
            {
                "update_id": 30,
                "message": {
                    "text": "/start",
                    "chat": {"id": -100123456, "type": "group"},
                    "from": {"id": 123456789, "first_name": "Ada"},
                },
            },
            {
                "update_id": 31,
                "message": {
                    "text": "/start",
                    "chat": {"id": 123456789, "type": "private"},
                    "from": {"id": 987654321, "first_name": "Mallory"},
                },
            },
        ]
    )

    assert results == []
    assert sent == []
    assert not (tmp_path / "telegram_subscribers.json").exists()
    assert not (tmp_path / "telegram_contacts.json").exists()


def test_weekly_telegram_delivery_is_idempotent_and_removes_blocked_chats(tmp_path, monkeypatch):
    subscribers_path = tmp_path / "telegram_subscribers.json"
    delivery_state_path = tmp_path / "telegram_delivery_state.json"
    subscribers_path.write_text('["111111111", "222222222"]', encoding="utf-8")
    monkeypatch.setattr(notifications, "NOTIFICATION_SUSPENSION_PATH", tmp_path / "suspension.json")
    monkeypatch.setattr("src.gui.TELEGRAM_SUBSCRIBERS_PATH", subscribers_path)
    monkeypatch.setattr("src.gui.TELEGRAM_DELIVERY_STATE_PATH", delivery_state_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_BROADCAST_DELAY_SECONDS", "0")
    monkeypatch.setattr("src.gui._weekly_telegram_message", lambda reference_date=None: "Digest settimanale")
    sent = []

    def send(chat_id, text, **_):
        if chat_id == "222222222":
            raise TelegramDeliveryError("bot was blocked", blocked=True)
        sent.append((chat_id, text))

    monkeypatch.setattr("src.gui._send_telegram_message", send)
    delivery_day = date(2026, 7, 10)

    first = send_weekly_telegram_digest_to_subscribers(delivery_day)
    second = send_weekly_telegram_digest_to_subscribers(delivery_day)
    state = json.loads(delivery_state_path.read_text(encoding="utf-8"))

    assert first == {"sent": 1, "failed": 1, "skipped": 0}
    assert second == {"sent": 0, "failed": 0, "skipped": 1}
    assert sent == [("111111111", "Digest settimanale")]
    assert json.loads(subscribers_path.read_text(encoding="utf-8")) == ["111111111"]
    assert state["weekly_delivery_by_chat"] == {"111111111": "2026-W28"}


def test_telegram_updates_returns_sanitized_errors_for_webhook_and_invalid_payloads(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code

        def raise_for_status(self):
            return None

        def json(self):
            if isinstance(self.payload, Exception):
                raise self.payload
            return self.payload

    response = FakeResponse(
        {"ok": False, "description": "Conflict: secret-token webhook is active"}
    )
    monkeypatch.setattr(gui.httpx, "get", lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError, match="webhook attivo") as webhook_error:
        gui._telegram_updates()

    assert "secret-token" not in str(webhook_error.value)

    response.payload = {"ok": True, "result": "not-an-update-list"}
    with pytest.raises(RuntimeError, match="aggiornamenti non validi") as invalid_error:
        gui._telegram_updates()

    assert "secret-token" not in str(invalid_error.value)

    conflict_response = FakeResponse(
        {"ok": False, "description": "Conflict: terminated by other getUpdates request"},
        status_code=409,
    )
    monkeypatch.setattr(gui.httpx, "get", lambda *args, **kwargs: conflict_response)

    with pytest.raises(gui.TelegramPollingConflictError, match="altro processo") as conflict_error:
        gui._telegram_updates()

    assert "secret-token" not in str(conflict_error.value)
