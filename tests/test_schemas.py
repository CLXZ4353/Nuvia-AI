from datetime import date
import re

import pytest
from pydantic import ValidationError

from src.schemas import Category, Digest, DigestEntry


def make_valid_entry(**overrides) -> dict:
    base = {
        "title": "GPT-5 supera GPT-4 su tutti i benchmark MMLU",
        "source": "OpenAI Research",
        "url": "https://openai.com/research/gpt5",
        "date": date.today(),
        "summary": (
            "OpenAI ha rilasciato GPT-5. Il modello ottiene score del 92% su MMLU. "
            "Supera i modelli precedenti su task di ragionamento e codice."
        ),
        "perche_conta": (
            "I developer che usano l'API OpenAI avranno accesso a capacità "
            "significativamente migliorate per task complessi."
        ),
        "category": Category.NUOVO_MODELLO,
        "relevance_score": 5,
    }
    base.update(overrides)
    return base


def test_valid_entry():
    entry = DigestEntry(**make_valid_entry())
    assert entry.relevance_score == 5


def test_title_is_shortened_to_eighty_characters():
    entry = DigestEntry(**make_valid_entry(title="A" * 81))

    assert entry.title == "A" * 80


def test_title_keeps_only_letters_numbers_and_spaces():
    entry = DigestEntry(
        **make_valid_entry(title="GPT-5: l'évolution des modèles! 🤖 / release [beta]")
    )

    assert entry.title == "GPT 5 l evolution des modeles release beta"
    assert re.fullmatch(r"[A-Za-z0-9 ]+", entry.title)


def test_score_out_of_range():
    with pytest.raises(ValidationError):
        DigestEntry(**make_valid_entry(relevance_score=6))


def test_score_below_editorial_threshold():
    with pytest.raises(ValidationError, match="tra 3 e 5"):
        DigestEntry(**make_valid_entry(relevance_score=2))


def test_future_publication_date_is_rejected():
    with pytest.raises(ValidationError, match="futura"):
        DigestEntry(**make_valid_entry(date=date(2099, 1, 1)))


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError, match="Extra inputs"):
        DigestEntry(**make_valid_entry(unverified_claim="no"))


def test_digest_too_many_entries():
    entries = [DigestEntry(**make_valid_entry(url=f"https://example.com/{index}")) for index in range(11)]
    with pytest.raises(ValidationError):
        Digest(
            generated_at="2026-06-16T07:00:00",
            run_date=date.today(),
            entries=entries,
            sources_fetched=10,
            sources_failed=[],
        )
