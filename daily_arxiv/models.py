"""Shared data models for the daily arXiv pipeline."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RawPaper(BaseModel):
    model_config = ConfigDict(extra="ignore")

    arxiv_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    link: str
    abstract: str
    primary_category: str
    arxiv_categories: list[str] = Field(default_factory=list)
    matched_categories: list[str] = Field(default_factory=list)
    field: str
    fields: list[str] = Field(default_factory=list)
    listing_date: str
    published_at: str | None = None
    updated_at: str | None = None

    @field_validator("arxiv_id", "title", "link", "abstract", "primary_category", "field", "listing_date")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value

    @field_validator("arxiv_categories", "matched_categories", "fields")
    @classmethod
    def _clean_string_list(cls, value: list[Any]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

    @model_validator(mode="after")
    def _fill_category_and_field_lists(self) -> "RawPaper":
        if not self.arxiv_categories:
            self.arxiv_categories = [self.primary_category]
        if not self.matched_categories:
            self.matched_categories = [self.primary_category]
        if not self.fields:
            self.fields = [self.field]
        if self.field not in self.fields:
            self.fields = [self.field, *self.fields]
        return self


class SummaryResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title_kor: str
    summary: str
    keywords: list[str]

    @field_validator("title_kor", "summary")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value

    @field_validator("keywords")
    @classmethod
    def _validate_keywords(cls, value: list[Any]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            keyword = str(item).strip()
            if keyword and keyword not in cleaned:
                cleaned.append(keyword)
        if len(cleaned) != 5:
            raise ValueError("exactly five unique keywords are required")
        return cleaned


class SummarizedPaper(RawPaper):
    title_kor: str
    summary: str
    keywords: list[str]

    @field_validator("keywords")
    @classmethod
    def _validate_paper_keywords(cls, value: list[str]) -> list[str]:
        cleaned = [keyword.strip() for keyword in value if keyword.strip()]
        if len(cleaned) != 5:
            raise ValueError("exactly five keywords are required")
        return cleaned
