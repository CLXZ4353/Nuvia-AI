from __future__ import annotations

import argparse
import logging

from src.config import load_config
from src.agent import _filter_allowed_candidate_items, _filter_recent_candidate_items
from src.tools.fetch import fetch_sources
from src.tools.sources import resolve_sources


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)


def check_sources(config_path: str = "config.yaml") -> tuple[int, list[str], int, int]:
    config = load_config(config_path)
    config["sources"] = resolve_sources(config, config_path)
    items, failed_sources = fetch_sources(config)
    allowed_domains = {
        domain.lower().removeprefix("www.")
        for domain in config.get("source_catalog", {}).get("authoritative_domains", [])
    }
    allowed_items = _filter_allowed_candidate_items(items, allowed_domains)
    recent_items = _filter_recent_candidate_items(allowed_items, config.get("recency", {}))
    return len(config["sources"]), failed_sources, len(items), len(recent_items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Controlla le fonti senza chiamare il modello")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--allow-failures", action="store_true")
    args = parser.parse_args(argv)

    total_sources, failed_sources, total_items, recent_items = check_sources(args.config)
    ok_sources = total_sources - len(failed_sources)
    print(f"Fonti operative: {ok_sources}/{total_sources}")
    print(f"Item raccolti: {total_items}")
    print(f"Item recenti eleggibili: {recent_items}")
    if failed_sources:
        print(f"Fonti in errore: {', '.join(failed_sources)}")
    else:
        print("Fonti in errore: nessuna")
    return 0 if args.allow_failures or not failed_sources else 1


if __name__ == "__main__":
    raise SystemExit(main())
