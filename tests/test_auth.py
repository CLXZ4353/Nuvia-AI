import http.client
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from urllib.parse import urlencode

import pytest

from src import auth
from src import gui
from src import notifications
from src.gui import DigestRequestHandler, _safe_next_path, process_telegram_start_updates, render_settings_html
from http.server import ThreadingHTTPServer


@pytest.fixture
def auth_store(tmp_path, monkeypatch):
    store_path = tmp_path / "auth_store.json"
    monkeypatch.setattr(auth, "AUTH_STORE_PATH", store_path)
    monkeypatch.setattr(gui, "NEWSLETTER_SUBSCRIBERS_PATH", tmp_path / "newsletter_subscribers.json")
    monkeypatch.setattr(gui, "TELEGRAM_SUBSCRIBERS_PATH", tmp_path / "telegram_subscribers.json")
    monkeypatch.setattr(gui, "TELEGRAM_START_STATE_PATH", tmp_path / "telegram_start_state.json")
    monkeypatch.setattr(gui, "TELEGRAM_CONTACTS_PATH", tmp_path / "telegram_contacts.json")
    monkeypatch.setattr(gui, "TELEGRAM_GENERATION_STATE_PATH", tmp_path / "telegram_generation_state.json")
    monkeypatch.setattr(gui, "TELEGRAM_DELIVERY_STATE_PATH", tmp_path / "telegram_delivery_state.json")
    monkeypatch.setattr(notifications, "NOTIFICATION_SUSPENSION_PATH", tmp_path / "suspension.json")
    return store_path


def _register_default_user():
    return auth.register_user(
        "Ada",
        "Lovelace",
        "ada@example.com",
        "SecurePass1!",
        "SecurePass1!",
        email_notifications=True,
        telegram_notifications=True,
    )


def test_registration_validates_required_fields_password_and_confirmation(auth_store):
    invalid = auth.register_user("", "", "not-an-email", "debole", "diversa")

    assert not invalid.ok
    assert "nome" in invalid.message.casefold()

    result = _register_default_user()

    assert result.ok
    assert result.user["email"] == "ada@example.com"
    assert result.user["preferences"] == {"email": True, "telegram": True}
    assert auth.authenticate_user("ADA@example.com", "SecurePass1!").ok
    assert not auth.authenticate_user("ada@example.com", "incorrect").ok

    stored = json.loads(auth_store.read_text(encoding="utf-8"))
    assert "SecurePass1!" not in auth_store.read_text(encoding="utf-8")
    assert stored["users"][0]["password_hash"].startswith("pbkdf2_sha256$")


def test_persistent_session_is_resolved_without_storing_the_raw_token(auth_store):
    registered = _register_default_user()

    token, expires_at = auth.create_session(registered.user["id"], persistent=True)

    assert expires_at > datetime.now(timezone.utc)
    assert auth.get_user_for_session(token)["id"] == registered.user["id"]
    store_contents = auth_store.read_text(encoding="utf-8")
    assert token not in store_contents

    auth.revoke_session(token)

    assert auth.get_user_for_session(token) is None


def test_email_and_notification_preferences_are_editable_and_rendered(auth_store, monkeypatch):
    registered = _register_default_user()
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "research_digest_bot")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")

    updated = auth.update_user_email(registered.user["id"], "new-address@example.com")
    preferences = auth.update_notification_preferences(
        registered.user["id"],
        email_notifications=False,
        telegram_notifications=True,
    )
    page = render_settings_html(user=preferences.user)

    assert updated.ok
    assert preferences.ok
    assert 'value="new-address@example.com"' in page
    assert 'action="/settings/profile"' in page
    assert 'action="/settings/preferences"' in page
    assert 'action="/logout"' in page
    assert 'id="settings-telegram-notifications"' not in page
    assert 'action="/settings/telegram/verify"' not in page
    assert "Apri nel browser" not in page
    assert "Collega nell’app Telegram" not in page
    assert "Copia comando" not in page
    assert ">Apri Telegram</a>" in page
    match = re.search(r'href="https://t\.me/research_digest_bot\?start=(connect_[A-Za-z0-9_-]+)"', page)
    assert match
    payload = match.group(1)
    assert registered.user["id"] not in payload
    assert auth.user_id_from_telegram_connect_payload(payload) == registered.user["id"]
    assert f'data-telegram-native="tg://resolve?domain=research_digest_bot&amp;start={payload}"' in page
    assert "window.location.assign(fallback)" in page


def test_registration_telegram_entry_has_only_the_start_link(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "research_digest_bot")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")

    page = gui.render_auth_html("register")

    assert 'href="https://t.me/research_digest_bot?start=onboarding"' in page
    assert 'data-telegram-native="tg://resolve?domain=research_digest_bot&amp;start=onboarding"' in page
    assert 'data-telegram-fallback="https://t.me/research_digest_bot?start=onboarding"' in page
    assert ">Apri Telegram</a>" in page
    assert 'id="notify-telegram"' not in page
    assert "Apri nel browser" not in page
    assert 'aria-label="Apri il bot Telegram"' in page
    assert "Collega nell’app Telegram" not in page


def test_telegram_connection_is_account_bound_and_unique(auth_store):
    first = _register_default_user()
    second = auth.register_user(
        "Grace",
        "Hopper",
        "grace@example.com",
        "SecurePass2!",
        "SecurePass2!",
    )

    payload = auth.telegram_connect_payload(first.user["id"])
    assert payload
    assert first.user["id"] not in payload
    assert auth.user_id_from_telegram_connect_payload(payload) == first.user["id"]

    connected = auth.connect_telegram_chat_from_payload(
        payload,
        "123456789",
        telegram_user_id="123456789",
        telegram_username="ada",
    )
    duplicate_payload = auth.telegram_connect_payload(second.user["id"])
    duplicate = auth.connect_telegram_chat_from_payload(
        duplicate_payload,
        "123456789",
        telegram_user_id="123456789",
    )

    assert connected.ok
    assert connected.user["telegram_connected"]
    assert auth.telegram_chat_id_for_user(first.user["id"]) == "123456789"
    assert auth.user_id_from_telegram_connect_payload(payload) is None
    assert not duplicate.ok
    assert auth.telegram_chat_id_for_user(second.user["id"]) is None


def test_account_telegram_start_enables_delivery_and_renders_single_link(auth_store, monkeypatch):
    enabled = _register_default_user()
    disabled = auth.register_user(
        "Linus",
        "Torvalds",
        "linus@example.com",
        "SecurePass3!",
        "SecurePass3!",
        telegram_notifications=False,
    )
    sent = []
    monkeypatch.setattr(gui, "_send_telegram_message", lambda chat_id, text, **_: sent.append((chat_id, text)))
    monkeypatch.setattr(gui, "_initial_telegram_message", lambda: "Digest iniziale")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "research_digest_bot")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    enabled_payload = auth.telegram_connect_payload(enabled.user["id"])
    disabled_payload = auth.telegram_connect_payload(disabled.user["id"])

    results = process_telegram_start_updates(
        [
            {
                "update_id": 1,
                "message": {
                    "text": f"/start {enabled_payload}",
                    "chat": {"id": 123456789, "type": "private"},
                    "from": {"id": 123456789, "username": "ada", "first_name": "Ada"},
                },
            },
            {
                "update_id": 2,
                "message": {
                    "text": f"/start {disabled_payload}",
                    "chat": {"id": 987654321, "type": "private"},
                    "from": {"id": 987654321, "username": "linus", "first_name": "Linus"},
                },
            },
        ]
    )
    enabled_user = auth.authenticate_user("ada@example.com", "SecurePass1!").user
    disabled_user = auth.authenticate_user("linus@example.com", "SecurePass3!").user
    page = render_settings_html(user=enabled_user)
    subscribers = json.loads(gui.TELEGRAM_SUBSCRIBERS_PATH.read_text(encoding="utf-8"))

    assert [result.ok for result in results] == [True, True]
    assert auth.telegram_chat_id_for_user(enabled.user["id"]) == "123456789"
    assert auth.telegram_chat_id_for_user(disabled.user["id"]) == "987654321"
    assert disabled_user["preferences"]["telegram"] is True
    assert subscribers == ["123456789", "987654321"]
    assert sent == [
        ("123456789", "Digest iniziale"),
        ("987654321", "Digest iniziale"),
    ]
    assert 'href="https://t.me/research_digest_bot?start=connect_' in page
    assert 'data-telegram-native="tg://resolve?domain=research_digest_bot&amp;start=connect_' in page
    assert ">Apri Telegram</a>" in page
    assert 'target="_blank"' not in page
    assert "Apri nel browser" not in page
    assert "Copia comando" not in page


def test_account_link_conflict_keeps_existing_binding_but_delivers_digest(auth_store, monkeypatch):
    owner = _register_default_user()
    target = auth.register_user(
        "Grace",
        "Hopper",
        "grace@example.com",
        "SecurePass2!",
        "SecurePass2!",
    )
    assert auth.connect_telegram_chat(owner.user["id"], "123456789").ok
    payload = auth.telegram_connect_payload(target.user["id"])
    sent = []
    monkeypatch.setattr(gui, "_initial_telegram_message", lambda: "Digest corrente")
    monkeypatch.setattr(gui, "_send_telegram_message", lambda chat_id, text, **_: sent.append((chat_id, text)))

    results = process_telegram_start_updates(
        [
            {
                "update_id": 10,
                "message": {
                    "text": f"/start {payload}",
                    "chat": {"id": 123456789, "type": "private"},
                    "from": {"id": 123456789, "first_name": "Ada"},
                },
            }
        ]
    )

    assert results[0].delivered
    assert "resta associata" in results[0].message
    assert sent == [("123456789", "Digest corrente")]
    assert auth.telegram_chat_id_for_user(owner.user["id"]) == "123456789"
    assert auth.telegram_chat_id_for_user(target.user["id"]) is None


def test_account_link_start_resends_current_digest_after_a_receipt(auth_store, monkeypatch):
    registered = _register_default_user()
    assert auth.connect_telegram_chat(registered.user["id"], "123456789").ok
    gui.TELEGRAM_START_STATE_PATH.write_text(
        json.dumps(
            {
                "initial_delivery_receipts": {
                    "123456789": {"week": gui._telegram_week_key(datetime.now().date())}
                }
            }
        ),
        encoding="utf-8",
    )
    payload = auth.telegram_connect_payload(registered.user["id"])
    sent = []
    monkeypatch.setattr(gui, "_initial_telegram_message", lambda: "Digest corrente")
    monkeypatch.setattr(gui, "_send_telegram_message", lambda chat_id, text, **_: sent.append((chat_id, text)))

    results = process_telegram_start_updates(
        [
            {
                "update_id": 11,
                "message": {
                    "text": f"/start {payload}",
                    "chat": {"id": 123456789, "type": "private"},
                    "from": {"id": 123456789, "first_name": "Ada"},
                },
            }
        ]
    )

    assert results[0].delivered
    assert sent == [("123456789", "Digest corrente")]


def test_safe_next_path_allows_local_shared_routes_only():
    assert _safe_next_path("/share/article-123?ref=email") == "/share/article-123?ref=email"
    assert _safe_next_path("https://attacker.example") == "/"
    assert _safe_next_path("//attacker.example") == "/"
    assert _safe_next_path("/\\attacker.example") == "/"


def test_telegram_status_endpoint_requires_login_and_returns_only_current_account_status(auth_store, monkeypatch):
    registered = _register_default_user()
    assert auth.connect_telegram_chat(registered.user["id"], "123456789").ok
    token, _ = auth.create_session(registered.user["id"])
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "research_digest_bot")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")

    with _test_server() as address:
        anonymous_status, _, anonymous_body = _request(address, "GET", "/api/telegram/status")
        status, _, body = _request(
            address,
            "GET",
            "/api/telegram/status",
            cookie=f"research_digest_session={token}",
        )

    assert anonymous_status == 401
    assert json.loads(anonymous_body) == {"ok": False, "message": "Accedi per verificare Telegram."}
    assert status == 200
    assert json.loads(body) == {
        "ok": True,
        "configured": True,
        "connected": True,
        "notifications_enabled": True,
    }


@contextmanager
def _test_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), DigestRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _request(address, method, path, fields=None, cookie=None, json_payload=None):
    headers = {}
    body = None
    if json_payload is not None:
        body = json.dumps(json_payload)
        headers["Content-Type"] = "application/json"
    elif fields is not None:
        body = urlencode(fields)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    payload = response.read().decode("utf-8")
    result = response.status, dict(response.getheaders()), payload
    connection.close()
    return result


def test_login_remember_me_cookie_settings_update_and_logout(auth_store):
    registered = _register_default_user()
    assert auth.connect_telegram_chat(registered.user["id"], "555666777").ok
    gui._save_subscriber("ada@example.com")

    with _test_server() as address:
        status, headers, response = _request(
            address,
            "POST",
            "/api/auth/login",
            json_payload={"email": "ada@example.com", "password": "SecurePass1!", "persistent": True},
        )

        assert status == 200
        assert json.loads(response)["user"]["email"] == "ada@example.com"
        set_cookie = headers["Set-Cookie"]
        assert "HttpOnly" in set_cookie
        assert "SameSite=Lax" in set_cookie
        assert "Max-Age=" in set_cookie
        cookies = SimpleCookie()
        cookies.load(set_cookie)
        cookie_value = cookies["research_digest_session"].value
        cookie_header = f"research_digest_session={cookie_value}"

        status, _, page = _request(address, "GET", "/settings", cookie=cookie_header)
        assert status == 200
        assert '<script type="module" src="/assets/js/app.js?v=20260722-login-chat-layout"></script>' in page
        assert 'id="settings-page"' in page
        assert 'value="ada@example.com"' not in page

        status, _, response = _request(address, "GET", "/api/settings", cookie=cookie_header)
        assert status == 200
        assert json.loads(response)["settings"]["profile"]["email"] == "ada@example.com"

        status, _, response = _request(
            address,
            "POST",
            "/api/settings/profile",
            cookie=cookie_header,
            json_payload={"email": "ada+updated@example.com"},
        )
        assert status == 200
        assert json.loads(response)["settings"]["profile"]["email"] == "ada+updated@example.com"
        assert auth.get_user_for_session(cookie_value)["email"] == "ada+updated@example.com"
        subscribers = json.loads(gui.NEWSLETTER_SUBSCRIBERS_PATH.read_text(encoding="utf-8"))
        assert "ada+updated@example.com" in subscribers
        assert "ada@example.com" not in subscribers

        status, _, response = _request(
            address,
            "POST",
            "/api/settings/preferences",
            cookie=cookie_header,
            json_payload={"email": True, "telegram": True},
        )
        assert status == 200
        assert json.loads(response)["settings"]["preferences"] == {"email": True, "telegram": True}
        subscribers = json.loads(gui.NEWSLETTER_SUBSCRIBERS_PATH.read_text(encoding="utf-8"))
        assert "ada+updated@example.com" in subscribers
        telegram_subscribers = json.loads(gui.TELEGRAM_SUBSCRIBERS_PATH.read_text(encoding="utf-8"))
        assert telegram_subscribers == ["555666777"]

        status, _, response = _request(
            address,
            "POST",
            "/api/settings/preferences",
            cookie=cookie_header,
            json_payload={"email": False, "telegram": True},
        )
        assert status == 200
        assert json.loads(response)["settings"]["preferences"] == {"email": False, "telegram": True}
        subscribers = json.loads(gui.NEWSLETTER_SUBSCRIBERS_PATH.read_text(encoding="utf-8"))
        assert "ada+updated@example.com" not in subscribers
        telegram_subscribers = json.loads(gui.TELEGRAM_SUBSCRIBERS_PATH.read_text(encoding="utf-8"))
        assert telegram_subscribers == ["555666777"]

        status, headers, response = _request(address, "POST", "/api/auth/logout", cookie=cookie_header)
        assert status == 200
        assert json.loads(response)["ok"] is True
        assert "Max-Age=0" in headers["Set-Cookie"]

        status, _, response = _request(address, "GET", "/api/settings", cookie=cookie_header)
        assert status == 401
        assert json.loads(response)["ok"] is False
