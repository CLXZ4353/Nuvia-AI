from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import feedparser
import yaml
from dotenv import load_dotenv

from src.agent import run_agent
from src.config import project_now, project_today
from src.schemas import Category, Digest, DigestEntry, normalize_news_title
from src.tools.delivery import deliver_to_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)


def run_demo(config_path: str = "config.yaml") -> Digest:
    """Run a deterministic offline digest from the bundled sample feed."""
    sample_feed = Path("sample-output/sample-feed.xml")
    if not sample_feed.exists():
        raise FileNotFoundError(f"Feed demo non trovato: {sample_feed}")

    config_file = Path(config_path)
    config = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {} if config_file.exists() else {}

    feed = feedparser.parse(sample_feed.read_text(encoding="utf-8"))
    entries = []
    for item in feed.entries[:3]:
        title = item.get("title", "").strip()
        summary = item.get("summary", "").strip()
        entries.append(
            DigestEntry(
                title=normalize_news_title(title),
                source=feed.feed.get("title", "Sample Feed"),
                url=item.get("link", ""),
                date=date.fromisoformat(item.get("published", project_today(config).isoformat())[:10]),
                summary=summary,
                perche_conta=(
                    "Mostra il formato finale del digest senza chiamate di rete o modello."
                ),
                category=Category.AGENTI,
                relevance_score=4,
            )
        )

    digest = Digest(
        generated_at=project_now(config).isoformat(),
        run_date=project_today(config),
        entries=entries,
        sources_fetched=1,
        sources_failed=[],
        is_partial=len(entries) < 3,
        partial_reason="Demo offline con meno di 3 voci" if len(entries) < 3 else None,
    )

    output_dir = "out"
    if config_file.exists():
        output_dir = config.get("demo", {}).get("out_dir", output_dir)
    output_path = deliver_to_file(digest, output_dir)
    logging.info("Demo offline completata: %s", output_path)
    return digest


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Research Digest Agent")
    parser.add_argument("config_arg", nargs="?", help="Percorso config, compatibile con python -m src.main config.yaml")
    parser.add_argument("--config", default=None, help="Percorso del file YAML di configurazione")
    parser.add_argument("--demo", action="store_true", help="Esegue la demo offline senza rete e senza API key")
    args = parser.parse_args(argv)
    config_path = args.config or args.config_arg or "config.yaml"

    try:
        digest = run_demo(config_path) if args.demo else run_agent(config_path)
        print(f"\nDigest completato: {len(digest.entries)} voci")
        if digest.is_partial:
            print(f"Parziale: {digest.partial_reason}")
        return 0
    except Exception as exc:
        logging.exception("Run fallita: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
