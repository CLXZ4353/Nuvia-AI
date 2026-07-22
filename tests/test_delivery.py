from datetime import date

from src.schemas import Category, Digest, DigestEntry
from src.tools.delivery import deliver_to_file, merge_with_existing_daily_digest, render_markdown, save_failed_digest


def test_render_markdown_contains_entry():
    entry = DigestEntry(
        title="Nuovo benchmark per agenti AI",
        source="Example Lab",
        url="https://example.com/benchmark",
        date=date(2026, 6, 16),
        summary="Il benchmark valuta agenti su task realistici. I risultati mostrano lacune nei modelli attuali.",
        perche_conta="Aiuta i team a scegliere modelli più affidabili per workflow agentici.",
        category=Category.AGENTI,
        relevance_score=4,
    )
    digest = Digest(
        generated_at="2026-06-16T07:00:00",
        run_date=date(2026, 6, 16),
        entries=[entry],
        sources_fetched=1,
        sources_failed=[],
    )

    markdown = render_markdown(digest)

    assert "Nuovo benchmark per agenti AI" in markdown
    assert "https://example.com/benchmark" in markdown
    assert "**Categoria:** agenti" in markdown


def test_deliver_to_file_uses_stable_daily_digest_path(tmp_path):
    digest = Digest(
        generated_at="2026-06-16T07:00:00",
        run_date=date(2026, 6, 16),
        entries=[],
        sources_fetched=1,
        sources_failed=[],
        is_partial=True,
        partial_reason="Test",
    )

    first_path = deliver_to_file(digest, str(tmp_path))
    second_path = deliver_to_file(digest, str(tmp_path))

    assert first_path == second_path
    assert first_path.name == "digest_2026-06-16.md"
    assert first_path.exists()
    assert first_path.read_text(encoding="utf-8") == second_path.read_text(encoding="utf-8")


def test_merge_with_existing_daily_digest_keeps_previous_entries(tmp_path):
    previous = Digest(
        generated_at="2026-06-16T07:00:00",
        run_date=date(2026, 6, 16),
        entries=[
            DigestEntry(
                title="Previous English Title",
                source="Example Lab",
                url="https://example.com/previous",
                date=date(2026, 6, 16),
                summary="La prima frase descrive il risultato. La seconda frase aggiunge il contesto.",
                perche_conta="Mantiene visibili le voci gia trovate.",
                category=Category.AGENTI,
                relevance_score=4,
            )
        ],
        sources_fetched=1,
        sources_failed=[],
    )
    deliver_to_file(previous, str(tmp_path))
    current = Digest(
        generated_at="2026-06-16T08:00:00",
        run_date=date(2026, 6, 16),
        entries=[
            DigestEntry(
                title="Current English Title",
                source="Example Lab",
                url="https://example.com/current",
                date=date(2026, 6, 16),
                summary="La prima frase descrive la nuova voce. La seconda frase aggiunge il contesto.",
                perche_conta="Aggiunge le novita senza perdere il pregresso.",
                category=Category.BENCHMARK,
                relevance_score=5,
            )
        ],
        sources_fetched=1,
        sources_failed=[],
    )

    merged = merge_with_existing_daily_digest(current, str(tmp_path))

    assert {entry.title for entry in merged.entries} == {"Previous English Title", "Current English Title"}


def test_render_markdown_displays_multi_word_category_without_underscore():
    entry = DigestEntry(
        title="Nuovo modello efficiente",
        source="Example Lab",
        url="https://example.com/model",
        date=date(2026, 6, 16),
        summary="La prima frase descrive il risultato. La seconda frase aggiunge contesto.",
        perche_conta="Aiuta a leggere le categorie in modo piu chiaro.",
        category=Category.NUOVO_MODELLO,
        relevance_score=4,
    )
    digest = Digest(
        generated_at="2026-06-16T07:00:00",
        run_date=date(2026, 6, 16),
        entries=[entry],
        sources_fetched=1,
        sources_failed=[],
    )

    markdown = render_markdown(digest)

    assert "**Categoria:** nuovo modello" in markdown
    assert "nuovo_modello" not in markdown


def test_save_failed_digest_does_not_overwrite_same_day_digest(tmp_path):
    digest = Digest(
        generated_at="2026-06-16T07:00:00",
        run_date=date(2026, 6, 16),
        entries=[],
        sources_fetched=1,
        sources_failed=[],
        is_partial=True,
        partial_reason="Test",
    )

    first_path = save_failed_digest(digest, str(tmp_path))
    second_path = save_failed_digest(digest, str(tmp_path))

    assert first_path != second_path
    assert first_path.exists()
    assert second_path.exists()
