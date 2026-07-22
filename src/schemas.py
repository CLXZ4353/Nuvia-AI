from __future__ import annotations

from datetime import date
from enum import Enum
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


NEWS_TITLE_MAX_LENGTH = 80
NEWS_TITLE_FALLBACK = "Notizia"


def normalize_news_title(value: object) -> str:
    """Keeps card titles short and limited to ordinary ASCII words."""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_title = decomposed.encode("ascii", "ignore").decode("ascii")
    clean_title = " ".join(re.sub(r"[^A-Za-z0-9]+", " ", ascii_title).split())
    if not clean_title:
        return NEWS_TITLE_FALLBACK
    if len(clean_title) <= NEWS_TITLE_MAX_LENGTH:
        return clean_title

    bounded_title = clean_title[:NEWS_TITLE_MAX_LENGTH].rstrip()
    last_space = bounded_title.rfind(" ")
    return bounded_title[:last_space].rstrip() if last_space > 0 else bounded_title


class Category(str, Enum):
    NUOVO_MODELLO = "nuovo_modello"
    PAPER_RICERCA = "paper_ricerca"
    BENCHMARK = "benchmark"
    TECNICA_TRAINING = "tecnica_training"
    MULTIMODALE = "multimodale"
    AGENTI = "agenti"
    SICUREZZA_ALIGNMENT = "sicurezza_alignment"
    EFFICIENZA = "efficienza"


class DigestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str
    source: str
    url: HttpUrl
    date: date
    summary: str = Field(min_length=1)
    perche_conta: str = Field(min_length=1)
    category: Category
    relevance_score: int

    @field_validator("title")
    @classmethod
    def title_is_short_and_plain(cls, v: str) -> str:
        return normalize_news_title(v)

    @field_validator("relevance_score")
    @classmethod
    def score_range(cls, v: int) -> int:
        if not (3 <= v <= 5):
            raise ValueError("relevance_score deve essere tra 3 e 5")
        return v

    @field_validator("source")
    @classmethod
    def source_not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("source non può essere vuota")
        return v

    @field_validator("date")
    @classmethod
    def date_not_in_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("date non può essere futura")
        return v

    @field_validator("summary")
    @classmethod
    def summary_length(cls, v: str) -> str:
        if not (2 <= _sentence_count(v) <= 4):
            raise ValueError("summary deve avere 2-4 frasi")
        return v

    @field_validator("perche_conta")
    @classmethod
    def perche_conta_length(cls, v: str) -> str:
        if not (1 <= _sentence_count(v) <= 2):
            raise ValueError("perche_conta deve avere 1-2 frasi")
        return v


def _sentence_count(value: str) -> int:
    return len([part for part in re.split(r"[.!?]+(?:\s+|$)", value.strip()) if part.strip()])


class Digest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat: str = "ricerca_ai_modelli"
    generated_at: str
    run_date: date
    entries: list[DigestEntry]
    sources_fetched: int
    sources_failed: list[str]
    is_partial: bool = False
    partial_reason: str | None = None

    @field_validator("entries")
    @classmethod
    def entries_count(cls, v: list[DigestEntry]) -> list[DigestEntry]:
        if len(v) > 10:
            raise ValueError("Il digest non può avere più di 10 voci")
        return v
