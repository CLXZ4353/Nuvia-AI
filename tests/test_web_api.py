"""HTTP-level contract tests for the browser-facing JSON API.

These tests deliberately use the real ``ThreadingHTTPServer`` request handler.
All persistence paths are redirected into pytest's temporary directory so the
suite can exercise session cookies and account-scoped storage safely.
"""

from __future__ import annotations

import http.client
import json
import threading
from datetime import date, timedelta
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer

import pytest

from src import auth, gui, notifications


@pytest.fixture
def web_state(tmp_path, monkeypatch):
    """Isolate every local store used by the web request handler."""

    data_dir = tmp_path / "data"
    monkeypatch.setattr(auth, "AUTH_STORE_PATH", data_dir / "auth_store.json")
    monkeypatch.setattr(gui, "SAVED_NEWS_PATH", data_dir / "saved_news.json")
    monkeypatch.setattr(gui, "NEWSLETTER_SUBSCRIBERS_PATH", data_dir / "newsletter_subscribers.json")
    monkeypatch.setattr(gui, "NEWSLETTER_OUTBOX_DIR", data_dir / "newsletter_outbox")
    monkeypatch.setattr(gui, "TELEGRAM_SUBSCRIBERS_PATH", data_dir / "telegram_subscribers.json")
    monkeypatch.setattr(gui, "TELEGRAM_START_STATE_PATH", data_dir / "telegram_start_state.json")
    monkeypatch.setattr(gui, "TELEGRAM_POLL_LOCK_PATH", data_dir / "telegram_poller.lock")
    monkeypatch.setattr(gui, "TELEGRAM_CONTACTS_PATH", data_dir / "telegram_contacts.json")
    monkeypatch.setattr(gui, "TELEGRAM_GENERATION_STATE_PATH", data_dir / "telegram_generation_state.json")
    monkeypatch.setattr(gui, "TELEGRAM_DELIVERY_STATE_PATH", data_dir / "telegram_delivery_state.json")
    monkeypatch.setattr(notifications, "NOTIFICATION_SUSPENSION_PATH", data_dir / "suspension.json")
    monkeypatch.setattr(
        notifications,
        "USER_NOTIFICATION_SUSPENSIONS_PATH",
        data_dir / "user_notification_suspensions.json",
    )

    # The static file tests must not rely on an asset already present in the
    # repository, and must prove that a sibling private file is unreachable.
    web_root = tmp_path / "web"
    assets = web_root / "assets"
    (assets / "css").mkdir(parents=True)
    (assets / "css" / "styles.css").write_text(".app { color: teal; }", encoding="utf-8")
    (web_root / "index.html").write_text("<!doctype html><title>Test UI</title>", encoding="utf-8")
    (tmp_path / "private.txt").write_text("not a public asset", encoding="utf-8")
    monkeypatch.setattr(gui, "WEB_ASSET_ROOT", web_root)
    monkeypatch.setattr(gui, "WEB_INDEX_PATH", web_root / "index.html")

    # Make the runtime configuration deterministic and keep local developer
    # environment variables from affecting the API assertions.
    monkeypatch.setattr(
        gui,
        "load_config",
        lambda _path: {
            "web": {
                "api_base_url": "/api/v2",
                "request_timeout_ms": 2_500,
                "environment": "test",
                "timezone": "Europe/Rome",
            }
        },
    )
    monkeypatch.setattr(gui, "public_firebase_config", lambda: {"enabled": False, "configured": False})
    for name in (
        "RESEARCH_DIGEST_API_BASE_URL",
        "RESEARCH_DIGEST_REQUEST_TIMEOUT_MS",
        "RESEARCH_DIGEST_ENV",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_USERNAME",
        "SMTP_HOST",
        "SMTP_FROM",
    ):
        monkeypatch.delenv(name, raising=False)

    return data_dir


@pytest.fixture
def http_server():
    """Run the real handler on an ephemeral local port for one test."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), gui.DigestRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _request(address, method, path, *, payload=None, raw_body=None, headers=None):
    request_headers = dict(headers or {})
    body = raw_body
    if payload is not None:
        body = json.dumps(payload)
        request_headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read().decode("utf-8")
    connection.close()
    return result


def _json_response(result):
    status, headers, body = result
    assert headers["Content-Type"].startswith("application/json")
    return status, headers, json.loads(body)


def _register_user(email="ada@example.com"):
    result = auth.register_user("Ada", "Lovelace", email, "SecurePass1!", "SecurePass1!")
    assert result.ok and result.user is not None
    return result.user


def _session_cookie(user):
    token, _ = auth.create_session(user["id"])
    return f"{gui.SESSION_COOKIE_NAME}={token}"


def _cookie_from_response(headers):
    cookie = SimpleCookie()
    cookie.load(headers["Set-Cookie"])
    return f"{gui.SESSION_COOKIE_NAME}={cookie[gui.SESSION_COOKIE_NAME].value}"


def _digest_view(digest_date, title, entry_title):
    return gui.DigestView(
        path=None,
        title=title,
        subtitle="Ricerca AI / modelli",
        warning=None,
        entries=[
            {
                "title": entry_title,
                "meta": "Fonte: Laboratorio Test - Data: 2026-07-16 - Categoria: modelli - Rilevanza: alta",
                "url": "https://example.com/research",
                "summary": "Una sintesi verificata della ricerca.",
                "why": "Aiuta a valutare le novita piu rilevanti.",
            }
        ],
        footer=[],
        digest_date=digest_date,
    )


@pytest.mark.parametrize("entry_count", [0, 1, 4])
def test_digest_adapter_preserves_zero_one_and_many_card_records(entry_count):
    view = _digest_view("2026-07-16", "Digest variabile", "Notizia iniziale")
    view.entries = [
        {
            **view.entries[0],
            "title": f"Notizia {index}",
            "url": f"https://example.com/research/{index}",
        }
        for index in range(entry_count)
    ]

    payload = gui._api_digest_payload(view)

    assert len(payload["items"]) == entry_count
    assert len({item["id"] for item in payload["items"]}) == entry_count


def test_config_and_static_assets_are_public_and_traversal_safe(web_state, http_server):
    status, _, config = _json_response(_request(http_server, "GET", "/api/config"))

    assert status == 200
    assert config == {
        "api_base_url": "/api/v2",
        "request_timeout_ms": 2_500,
        "environment": "test",
        "timezone": "Europe/Rome",
        "features": {"newsletter": True, "telegram": False, "push": False},
    }

    status, headers, stylesheet = _request(http_server, "GET", "/assets/css/styles.css")
    assert status == 200
    assert headers["Content-Type"].startswith("text/css")
    assert headers["Cache-Control"] == "public, max-age=0, must-revalidate"
    assert stylesheet == ".app { color: teal; }"

    status, _, body = _request(http_server, "GET", "/assets/%2e%2e/private.txt")
    assert status == 404
    assert "not a public asset" not in body

    status, _, prefixed_config = _json_response(_request(http_server, "GET", "/api/v2/config"))
    assert status == 200
    assert prefixed_config["api_base_url"] == "/api/v2"

    status, _, prefixed_me = _json_response(_request(http_server, "GET", "/api/v2/me"))
    assert status == 401
    assert prefixed_me["ok"] is False


def test_structured_digest_endpoints_require_auth_and_paginate_history(web_state, http_server, monkeypatch):
    current = _digest_view("2026-07-16", "Digest corrente", "Modello corrente")
    first = _digest_view("2026-07-15", "Digest del 15 luglio", "Prima notizia storica")
    second = _digest_view("2026-07-08", "Digest dell'8 luglio", "Seconda notizia storica")
    views = {"2026-07-15": first, "2026-07-08": second}
    history = [
        gui.DigestHistoryItem(
            id="date:2026-07-16",
            path="digest-2026-07-16.md",
            title=current.title,
            subtitle=current.subtitle,
            updated="16/07/2026 08:00",
            entry_count=1,
            digest_date="2026-07-16",
        ),
        gui.DigestHistoryItem(
            id="date:2026-07-15",
            path="digest-2026-07-15.md",
            title=first.title,
            subtitle=first.subtitle,
            updated="15/07/2026 08:00",
            entry_count=1,
            digest_date="2026-07-15",
        ),
        gui.DigestHistoryItem(
            id="date:2026-07-08",
            path="digest-2026-07-08.md",
            title=second.title,
            subtitle=second.subtitle,
            updated="08/07/2026 08:00",
            entry_count=1,
            digest_date="2026-07-08",
        ),
    ]
    monkeypatch.setattr(gui, "load_current_digest", lambda: current)
    monkeypatch.setattr(gui, "load_latest_digest", lambda: current)
    monkeypatch.setattr(gui, "load_digest_by_date", lambda value: views.get(value))
    monkeypatch.setattr(gui, "digest_history", lambda: history)

    status, _, anonymous = _json_response(_request(http_server, "GET", "/api/digest/current"))
    assert status == 401
    assert anonymous["ok"] is False

    user = _register_user()
    cookie = _session_cookie(user)
    headers = {"Cookie": cookie}

    status, _, current_payload = _json_response(
        _request(http_server, "GET", "/api/digest/current", headers=headers)
    )
    assert status == 200
    assert current_payload["ok"] is True
    assert current_payload["digest"]["digest_date"] == "2026-07-16"
    assert current_payload["digest"]["items"] == [
        {
            "id": current_payload["digest"]["items"][0]["id"],
            "title": "Modello corrente",
            "category": "modelli",
            "source": "Laboratorio Test",
            "url": "https://example.com/research",
            "summary": "Una sintesi verificata della ricerca.",
            "why": "Aiuta a valutare le novita piu rilevanti.",
            "publication_date": "2026-07-16",
            "relevance": "alta",
            "digest_date": "2026-07-16",
            "digest_title": "Digest corrente",
            "saved": False,
        }
    ]

    status, _, selected_payload = _json_response(
        _request(http_server, "GET", "/api/digest?date=2026-07-08", headers=headers)
    )
    assert status == 200
    assert selected_payload["digest"]["title"] == "AI Research Digest - 08/07/2026"

    status, _, first_page = _json_response(
        _request(http_server, "GET", "/api/digests?limit=1&offset=0", headers=headers)
    )
    assert status == 200
    assert first_page["total"] == 1
    assert first_page["offset"] == 0
    assert first_page["limit"] == 1
    assert first_page["next_offset"] is None
    assert [item["digest_date"] for item in first_page["items"]] == ["2026-07-08"]
    assert first_page["items"][0]["previews"][0]["title"] == "Seconda notizia storica"

    status, _, second_page = _json_response(
        _request(http_server, "GET", "/api/digests?limit=1&offset=1", headers=headers)
    )
    assert status == 200
    assert second_page["next_offset"] is None
    assert second_page["items"] == []


def test_current_week_moves_to_history_only_after_the_next_publication(monkeypatch):
    current = gui.DigestHistoryItem(
        id="date:2026-07-14",
        path="digest-2026-07-14.md",
        title="Settimana corrente",
        subtitle="",
        updated="14/07/2026 08:00",
        digest_date="2026-07-14",
    )
    previous = gui.DigestHistoryItem(
        id="date:2026-07-09",
        path="digest-2026-07-09.md",
        title="Settimana precedente",
        subtitle="",
        updated="09/07/2026 08:00",
        digest_date="2026-07-09",
    )
    next_week = gui.DigestHistoryItem(
        id="date:2026-07-21",
        path="digest-2026-07-21.md",
        title="Nuova settimana corrente",
        subtitle="",
        updated="21/07/2026 08:00",
        digest_date="2026-07-21",
    )
    published = [current, previous]
    monkeypatch.setattr(gui, "digest_history", lambda: published)

    assert [item.digest_date for item in gui.archived_digest_history()] == ["2026-07-09"]

    published.insert(0, next_week)

    assert [item.digest_date for item in gui.archived_digest_history()] == [
        "2026-07-14",
        "2026-07-09",
    ]


def test_json_auth_me_logout_and_malformed_json(web_state, http_server):
    status, _, anonymous = _json_response(_request(http_server, "GET", "/api/me"))
    assert status == 401
    assert anonymous["ok"] is False

    status, _, cross_site = _json_response(
        _request(
            http_server,
            "POST",
            "/api/v2/auth/login",
            payload={"email": "ada@example.com", "password": "SecurePass1!"},
            headers={"Origin": "https://untrusted.example"},
        )
    )
    assert status == 403
    assert cross_site["ok"] is False

    status, _, malformed = _json_response(
        _request(
            http_server,
            "POST",
            "/api/auth/register",
            raw_body="{not-json",
            headers={"Content-Type": "application/json"},
        )
    )
    assert status == 400
    assert malformed == {"ok": False, "message": "JSON non valido."}

    status, headers, registered = _json_response(
        _request(
            http_server,
            "POST",
            "/api/auth/register",
            payload={
                "first_name": "Ada",
                "last_name": "Lovelace",
                "email": "ada@example.com",
                "password": "SecurePass1!",
                "password_confirmation": "SecurePass1!",
                "email_notifications": True,
                "persistent": True,
            },
        )
    )
    assert status == 201
    assert registered["ok"] is True
    assert registered["user"]["email"] == "ada@example.com"
    assert "password" not in registered["user"]
    assert "HttpOnly" in headers["Set-Cookie"]
    assert "SameSite=Lax" in headers["Set-Cookie"]
    cookie = _cookie_from_response(headers)

    status, me_headers, me = _json_response(_request(http_server, "GET", "/api/me", headers={"Cookie": cookie}))
    assert status == 200
    assert me["user"]["email"] == "ada@example.com"
    assert me_headers["Cache-Control"] == "private, no-store"
    assert me_headers["Vary"] == "Cookie"

    status, headers, logged_out = _json_response(
        _request(http_server, "POST", "/api/auth/logout", headers={"Cookie": cookie})
    )
    assert status == 200
    assert logged_out["ok"] is True
    assert "Max-Age=0" in headers["Set-Cookie"]

    status, _, expired = _json_response(_request(http_server, "GET", "/api/me", headers={"Cookie": cookie}))
    assert status == 401
    assert expired["ok"] is False

    status, _, logged_in = _json_response(
        _request(
            http_server,
            "POST",
            "/api/auth/login",
            payload={"email": "ada@example.com", "password": "SecurePass1!"},
        )
    )
    assert status == 200
    assert logged_in["ok"] is True
    assert logged_in["user"]["email"] == "ada@example.com"


def test_saved_api_lists_saves_and_removes_account_scoped_news(web_state, http_server, monkeypatch):
    record = {
        "id": "n_1234567890abcdef12345678",
        "title": "Notizia da salvare",
        "meta": "Fonte: Fonte affidabile - Data: 2026-07-16 - Categoria: agenti - Rilevanza: media",
        "url": "https://example.com/saved-news",
        "summary": "Una sintesi per il test delle notizie salvate.",
        "why": "Verifica la persistenza lato server.",
        "digest_title": "Digest corrente",
        "digest_date": "2026-07-16",
    }
    monkeypatch.setattr(gui, "find_news_record", lambda news_id: dict(record) if news_id == record["id"] else None)
    owner = _register_user()
    other = _register_user("grace@example.com")
    owner_headers = {"Cookie": _session_cookie(owner)}
    other_headers = {"Cookie": _session_cookie(other)}

    status, _, saved = _json_response(
        _request(
            http_server,
            "POST",
            "/api/saved",
            payload={"news_id": record["id"], "action": "save"},
            headers=owner_headers,
        )
    )
    assert status == 200
    assert saved["saved"] is True
    assert saved["item"]["id"] == record["id"]

    status, _, owner_saved = _json_response(_request(http_server, "GET", "/api/saved", headers=owner_headers))
    assert status == 200
    assert [item["id"] for item in owner_saved["items"]] == [record["id"]]
    assert owner_saved["items"][0]["saved"] is True

    status, _, other_saved = _json_response(_request(http_server, "GET", "/api/saved", headers=other_headers))
    assert status == 200
    assert other_saved["items"] == []

    status, _, removed = _json_response(
        _request(
            http_server,
            "POST",
            "/api/saved",
            payload={"news_id": record["id"], "action": "remove"},
            headers=owner_headers,
        )
    )
    assert status == 200
    assert removed["saved"] is False

    status, _, owner_after_removal = _json_response(
        _request(http_server, "GET", "/api/saved", headers=owner_headers)
    )
    assert status == 200
    assert owner_after_removal["items"] == []


def test_settings_api_updates_profile_preferences_and_user_scoped_suspensions(web_state, http_server, monkeypatch):
    owner = _register_user()
    other = _register_user("grace@example.com")
    owner_headers = {"Cookie": _session_cookie(owner)}
    other_headers = {"Cookie": _session_cookie(other)}
    token = "p" * 40
    monkeypatch.setattr(
        gui,
        "fcm_token_registered_for_user",
        lambda candidate, user_id: candidate == token and user_id == owner["id"],
    )

    status, _, owner_push = _json_response(
        _request(
            http_server,
            "POST",
            "/api/push/status",
            payload={"token": token},
            headers=owner_headers,
        )
    )
    assert status == 200
    assert owner_push == {"ok": True, "registered": True}

    status, _, other_push = _json_response(
        _request(
            http_server,
            "POST",
            "/api/push/status",
            payload={"token": token},
            headers=other_headers,
        )
    )
    assert status == 200
    assert other_push == {"ok": True, "registered": False}

    status, _, profile = _json_response(
        _request(
            http_server,
            "POST",
            "/api/settings/profile",
            payload={"email": "ada+updated@example.com"},
            headers=owner_headers,
        )
    )
    assert status == 200
    assert profile["settings"]["profile"]["email"] == "ada+updated@example.com"

    status, _, preferences = _json_response(
        _request(
            http_server,
            "POST",
            "/api/settings/preferences",
            payload={"email": False, "telegram": True},
            headers=owner_headers,
        )
    )
    assert status == 200
    assert preferences["settings"]["preferences"] == {"email": False, "telegram": False}
    assert "Collega prima Telegram" in preferences["message"]

    owner_start = date.today() + timedelta(days=1)
    owner_end = owner_start + timedelta(days=2)
    status, _, owner_suspension = _json_response(
        _request(
            http_server,
            "POST",
            "/api/settings/suspension",
            payload={
                "action": "save",
                "start_date": owner_start.isoformat(),
                "end_date": owner_end.isoformat(),
            },
            headers=owner_headers,
        )
    )
    assert status == 200
    assert owner_suspension["suspension"]["configured"] is True
    assert owner_suspension["suspension"]["start"] == owner_start.isoformat()
    assert owner_suspension["suspension"]["end"] == owner_end.isoformat()

    status, _, other_settings = _json_response(_request(http_server, "GET", "/api/settings", headers=other_headers))
    assert status == 200
    assert other_settings["settings"]["suspension"]["configured"] is False

    other_start = owner_end + timedelta(days=1)
    other_end = other_start + timedelta(days=1)
    status, _, other_suspension = _json_response(
        _request(
            http_server,
            "POST",
            "/api/settings/suspension",
            payload={
                "action": "save",
                "start_date": other_start.isoformat(),
                "end_date": other_end.isoformat(),
            },
            headers=other_headers,
        )
    )
    assert status == 200
    assert other_suspension["suspension"]["configured"] is True

    status, _, cleared = _json_response(
        _request(
            http_server,
            "POST",
            "/api/settings/suspension",
            payload={"action": "clear"},
            headers=owner_headers,
        )
    )
    assert status == 200
    assert cleared["suspension"]["configured"] is False

    status, _, other_after_owner_clear = _json_response(
        _request(http_server, "GET", "/api/settings", headers=other_headers)
    )
    assert status == 200
    assert other_after_owner_clear["settings"]["suspension"]["configured"] is True


def test_assistant_api_requires_auth_and_forwards_only_validated_news_context(
    web_state,
    http_server,
    monkeypatch,
):
    captured = {}

    def fake_answer(message, context, history):
        captured.update({"message": message, "context": context, "history": history})
        return "La notizia descrive un nuovo modello, in parole semplici."

    monkeypatch.setattr(gui, "answer_news_question", fake_answer)
    payload = {
        "message": "Spiegamela in modo semplice",
        "context": {
            "title": "Nuovo modello AI",
            "content": "Sintesi completa.\n\nPerché conta: impatto verificato.",
            "source": "Laboratorio Test",
            "url": "https://example.com/research",
        },
        "history": [
            {"role": "system", "content": "Istruzione da ignorare"},
            {"role": "assistant", "content": "Che cosa vorresti approfondire di questa notizia?"},
        ],
    }

    status, _, anonymous = _json_response(
        _request(http_server, "POST", "/api/assistant", payload=payload)
    )
    assert status == 401
    assert anonymous["ok"] is False

    user = _register_user()
    headers = {"Cookie": _session_cookie(user)}
    status, _, response = _json_response(
        _request(http_server, "POST", "/api/assistant", payload=payload, headers=headers)
    )

    assert status == 200
    assert response == {
        "ok": True,
        "answer": "La notizia descrive un nuovo modello, in parole semplici.",
    }
    assert captured == {
        "message": "Spiegamela in modo semplice",
        "context": {
            "title": "Nuovo modello AI",
            "content": "Sintesi completa.\n\nPerché conta: impatto verificato.",
            "source": "Laboratorio Test",
            "url": "https://example.com/research",
        },
        "history": [
            {"role": "assistant", "content": "Che cosa vorresti approfondire di questa notizia?"},
        ],
    }

    comparison_payload = {
        **payload,
        "message": "Confronta questa notizia con la precedente",
        "comparison_context": {
            "title": "Modello precedente",
            "content": "Contesto precedente verificato.",
            "source": "Fonte precedente",
            "url": "https://example.com/previous",
        },
    }
    status, _, _ = _json_response(
        _request(http_server, "POST", "/api/assistant", payload=comparison_payload, headers=headers)
    )
    assert status == 200
    assert captured["context"]["comparison"]["title"] == "Modello precedente"

    invalid_payload = {**payload, "context": {**payload["context"], "url": "javascript:alert(1)"}}
    status, _, invalid = _json_response(
        _request(http_server, "POST", "/api/assistant", payload=invalid_payload, headers=headers)
    )
    assert status == 422
    assert invalid["ok"] is False


def test_assistant_api_returns_a_safe_provider_error(web_state, http_server, monkeypatch):
    user = _register_user()
    headers = {"Cookie": _session_cookie(user)}
    monkeypatch.setattr(
        gui,
        "answer_news_question",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("provider secret detail")),
    )

    status, _, response = _json_response(
        _request(
            http_server,
            "POST",
            "/api/assistant",
            payload={"message": "Riassumi", "context": {}, "history": []},
            headers=headers,
        )
    )

    assert status == 502
    assert response["ok"] is False
    assert response["message"] == "Nuvia non riesce a rispondere in questo momento. Riprova tra poco."
    assert "secret" not in response["message"]
