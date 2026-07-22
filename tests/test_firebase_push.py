from pathlib import Path

from src.firebase_push import (
    FirebasePushSettings,
    fcm_token_registered_for_user,
    public_firebase_config,
    remove_fcm_token,
    render_firebase_messaging_service_worker,
    save_fcm_token,
)


def test_public_firebase_config_exposes_only_web_push_values():
    settings = FirebasePushSettings(
        enabled=True,
        project_id="project-id",
        credentials_path=Path("secret-service-account.json"),
        token_store_path=Path("tokens.json"),
        default_topic="research-digest",
        web_config={
            "apiKey": "api-key",
            "projectId": "project-id",
            "messagingSenderId": "123456789",
            "appId": "1:123456789:web:abc",
        },
        vapid_key="vapid-public-key",
    )

    payload = public_firebase_config(settings)

    assert payload["configured"]
    assert payload["web"]["apiKey"] == "api-key"
    assert "credentials_path" not in payload
    assert "secret-service-account" not in str(payload)


def test_service_worker_is_noop_when_firebase_is_disabled():
    settings = FirebasePushSettings(
        enabled=False,
        project_id="",
        credentials_path=None,
        token_store_path=Path("tokens.json"),
        default_topic="research-digest",
        web_config={},
        vapid_key="",
    )

    script = render_firebase_messaging_service_worker(settings)

    assert "firebase.messaging" not in script
    assert "skipWaiting" in script


def test_save_fcm_token_dedupes_tokens(tmp_path):
    settings = FirebasePushSettings(
        enabled=True,
        project_id="project-id",
        credentials_path=None,
        token_store_path=tmp_path / "fcm_tokens.json",
        default_topic="research-digest",
        web_config={},
        vapid_key="",
    )
    token = "a" * 40

    first_count = save_fcm_token(token, settings)
    second_count = save_fcm_token(token, settings)

    assert first_count == 1
    assert second_count == 1
    assert token in settings.token_store_path.read_text(encoding="utf-8")


def test_fcm_token_status_and_removal_are_account_scoped(tmp_path):
    settings = FirebasePushSettings(
        enabled=True,
        project_id="project-id",
        credentials_path=None,
        token_store_path=tmp_path / "fcm_tokens.json",
        default_topic="research-digest",
        web_config={},
        vapid_key="",
    )
    token = "b" * 40
    first_user = "a" * 32
    second_user = "c" * 32

    save_fcm_token(token, settings, user_id=first_user)

    assert fcm_token_registered_for_user(token, first_user, settings)
    assert not fcm_token_registered_for_user(token, second_user, settings)
    assert not remove_fcm_token(token, settings, user_id=second_user)
    assert fcm_token_registered_for_user(token, first_user, settings)

    # An explicit activation on a shared browser transfers the device token
    # to the newly authenticated account; the previous account cannot remove
    # it afterwards.
    save_fcm_token(token, settings, user_id=second_user)
    assert not fcm_token_registered_for_user(token, first_user, settings)
    assert fcm_token_registered_for_user(token, second_user, settings)
    assert remove_fcm_token(token, settings, user_id=second_user)
    assert not fcm_token_registered_for_user(token, second_user, settings)
