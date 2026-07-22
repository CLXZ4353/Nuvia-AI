from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path

from src.schemas import Category, Digest, DigestEntry
from src.tools.urls import canonicalize_url

logger = logging.getLogger(__name__)


def render_markdown(digest: Digest) -> str:
    lines = [
        f"# AI Research Digest - {digest.run_date.strftime('%d/%m/%Y')}",
        f"*Beat: Ricerca AI / modelli - {len(digest.entries)} voci*",
        "",
    ]
    if digest.is_partial:
        lines.append(f"> Attenzione, digest parziale: {digest.partial_reason}")
        lines.append("")

    for index, entry in enumerate(digest.entries, 1):
        lines += [
            f"## {index}. {entry.title}",
            f"**Fonte:** {entry.source} - **Data:** {entry.date} - "
            f"**Categoria:** {_display_category(entry.category.value)} - **Rilevanza:** {entry.relevance_score}/5",
            f"**URL:** {entry.url}",
            "",
            f"**Sintesi:** {entry.summary}",
            "",
            f"**Perché conta:** {entry.perche_conta}",
            "",
            "---",
            "",
        ]

    lines += [
        f"*Generato il {digest.generated_at} - Fonti consultate: {digest.sources_fetched}*",
    ]
    if digest.sources_failed:
        lines.append(f"*Fonti in errore: {', '.join(digest.sources_failed)}*")
    return "\n".join(lines)


def deliver_to_file(digest: Digest, output_dir: str) -> Path:
    content = render_markdown(digest)
    output_path = _daily_digest_path(Path(output_dir), digest.run_date)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def merge_with_existing_daily_digest(digest: Digest, output_dir: str) -> Digest:
    existing_entries: list[DigestEntry] = []
    for path in _same_day_digest_paths(Path(output_dir), digest.run_date):
        existing_entries.extend(_read_digest_entries(path))
    if not existing_entries:
        return digest

    merged_entries = _dedupe_entries([*existing_entries, *digest.entries])
    merged_entries = sorted(merged_entries, key=_entry_sort_key, reverse=True)[:10]
    is_partial = len(merged_entries) < 3
    return Digest(
        beat=digest.beat,
        generated_at=digest.generated_at,
        run_date=digest.run_date,
        entries=merged_entries,
        sources_fetched=digest.sources_fetched,
        sources_failed=digest.sources_failed,
        is_partial=is_partial,
        partial_reason=(
            "Meno di 3 voci trovate con rilevanza sufficiente"
            if is_partial
            else None
        ),
    )


def save_failed_digest(digest: Digest, failed_dir: str = "data/failed") -> Path:
    output_path = _unique_digest_path(Path(failed_dir), f"{digest.run_date.isoformat()}_failed")
    output_path.write_text(render_markdown(digest), encoding="utf-8")
    logger.info("Digest salvato in failed: %s", output_path)
    return output_path


def _daily_digest_path(directory: Path, run_date: date) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"digest_{run_date.isoformat()}.md"


def _same_day_digest_paths(directory: Path, run_date: date) -> list[Path]:
    if not directory.exists():
        return []
    stem = f"digest_{run_date.isoformat()}"
    return sorted(directory.glob(f"{stem}*.md"), key=lambda path: path.stat().st_mtime)


def _read_digest_entries(path: Path) -> list[DigestEntry]:
    try:
        return _parse_markdown_entries(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning("Digest esistente non leggibile per merge giornaliero: %s (%s)", path, exc)
        return []


def _parse_markdown_entries(markdown: str) -> list[DigestEntry]:
    entries: list[DigestEntry] = []
    current: dict | None = None
    active_field: str | None = None

    for line in markdown.splitlines():
        if line.startswith("## "):
            if current:
                _append_entry(entries, current)
            current = {"title": line.removeprefix("## ").split(". ", 1)[-1]}
            active_field = None
            continue
        if current is None:
            continue
        if line.startswith("---") or line.startswith("*Generato") or line.startswith("*Fonti"):
            active_field = None
            continue

        stripped = line.strip()
        label, value = _split_label(stripped)
        if label == "fonte":
            current.update(_parse_meta_line(stripped))
            active_field = None
        elif label == "url":
            current["url"] = _extract_url(value)
            active_field = None
        elif label == "sintesi":
            current["summary"] = value
            active_field = "summary"
        elif label in {"perche conta", "perche' conta", "perchã© conta", "perchè conta", "perché conta"}:
            current["perche_conta"] = value
            active_field = "perche_conta"
        elif active_field and stripped:
            current[active_field] = f"{current.get(active_field, '')} {stripped}".strip()

    if current:
        _append_entry(entries, current)
    return entries


def _append_entry(entries: list[DigestEntry], payload: dict) -> None:
    try:
        entries.append(DigestEntry(**_clean_entry_payload(payload)))
    except Exception as exc:
        logger.warning("Voce esistente scartata durante merge giornaliero: %s", exc)


def _clean_entry_payload(payload: dict) -> dict:
    cleaned = {key: " ".join(str(value).split()) for key, value in payload.items()}
    if "category" in cleaned:
        cleaned["category"] = Category(_category_value(cleaned["category"]))
    if "relevance_score" in cleaned:
        cleaned["relevance_score"] = int(cleaned["relevance_score"])
    return cleaned


def _parse_meta_line(line: str) -> dict:
    without_bold = line.replace("**", "")
    match = re.search(
        r"Fonte:\s*(?P<source>.*?)\s+-\s+Data:\s*(?P<date>.*?)\s+-\s+Categoria:\s*(?P<category>.*?)\s+-\s+Rilevanza:\s*(?P<score>\d+)/5",
        without_bold,
    )
    if not match:
        return {}
    return {
        "source": match.group("source").strip(),
        "date": match.group("date").strip(),
        "category": match.group("category").strip(),
        "relevance_score": match.group("score").strip(),
    }


def _split_label(line: str) -> tuple[str | None, str]:
    match = re.match(r"^\*\*(?P<label>[^:]+):\*\*\s*(?P<value>.*)$", line)
    if not match:
        return None, line
    return match.group("label").strip().casefold(), match.group("value").strip()


def _extract_url(value: str) -> str:
    markdown_link = re.match(r"^\[(?P<label>[^\]]+)\]\((?P<url>[^)]+)\)$", value.strip())
    if markdown_link:
        return markdown_link.group("url").strip()
    return value.strip()


def _display_category(value: str) -> str:
    return " ".join(part for part in str(value or "").replace("_", " ").split())


def _category_value(value: str) -> str:
    return "_".join(str(value or "").strip().split()).casefold()


def _dedupe_entries(entries: list[DigestEntry]) -> list[DigestEntry]:
    unique: dict[str, DigestEntry] = {}
    for entry in entries:
        key = canonicalize_url(str(entry.url))
        previous = unique.get(key)
        if previous is None or _entry_sort_key(entry) > _entry_sort_key(previous):
            unique[key] = entry
    return list(unique.values())


def _entry_sort_key(entry: DigestEntry) -> tuple[int, int, str]:
    return entry.relevance_score, entry.date.toordinal(), entry.title


def _unique_digest_path(directory: Path, stem: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H%M%S")
    candidate = directory / f"digest_{stem}_{timestamp}.md"
    if not candidate.exists():
        return candidate

    counter = 2
    while True:
        candidate = directory / f"digest_{stem}_{timestamp}_{counter}.md"
        if not candidate.exists():
            return candidate
        counter += 1
