import yaml

from src.config import load_config


def test_load_config_resolves_paths_relative_to_config_file(tmp_path):
    config_path = tmp_path / "nested" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        yaml.safe_dump(
            {
                "beat": "ricerca_ai_modelli",
                "source_catalog": {"path": "source_catalog.yaml"},
                "sources": [],
                "delivery": {
                    "primary": {"type": "markdown_file", "path": "digests"},
                    "failed_path": "failed",
                },
                "state": {"db_path": "state/seen.db"},
                "demo": {"feed_path": "sample-feed.xml", "out_dir": "out"},
                "notifications": {
                    "firebase": {
                        "credentials_path": "firebase/service-account.json",
                        "token_store_path": "firebase/fcm_tokens.json",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(str(config_path))

    assert config["source_catalog"]["path"] == str(config_path.parent / "source_catalog.yaml")
    assert config["delivery"]["primary"]["path"] == str(config_path.parent / "digests")
    assert config["delivery"]["failed_path"] == str(config_path.parent / "failed")
    assert config["state"]["db_path"] == str(config_path.parent / "state" / "seen.db")
    assert config["demo"]["feed_path"] == str(config_path.parent / "sample-feed.xml")
    assert config["demo"]["out_dir"] == str(config_path.parent / "out")
    assert config["notifications"]["firebase"]["credentials_path"] == str(
        config_path.parent / "firebase" / "service-account.json"
    )
    assert config["notifications"]["firebase"]["token_store_path"] == str(
        config_path.parent / "firebase" / "fcm_tokens.json"
    )
