"""Export database rows into static JSON for the GitHub Pages web UI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from config.settings import Settings, load_settings
from daily_arxiv.database import FieldRow, KeywordRow, PaperRow, init_db


CATEGORY_LABELS = {
    "cs.RO": "Robotics",
    "cs.LG": "Machine Learning",
    "cs.CV": "Computer Vision",
    "stat.ML": "Machine Learning",
    "cs.AI": "Artificial Intelligence",
    "cs.CL": "Language",
}

FIELD_LABELS = {
    "robotics": "Robotics",
    "machine_learning": "Machine Learning",
    "computer_vision": "Computer Vision",
}


def web_data_path(settings: Settings) -> Path:
    return settings.web_output_dir / "data" / "site-data.json"


def _load_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def _paper_payload(row: PaperRow) -> dict[str, object]:
    fields = _load_json_list(row.fields_json) or [row.field]
    matched_categories = _load_json_list(row.matched_categories_json) or [row.primary_category]
    arxiv_categories = _load_json_list(row.arxiv_categories_json) or [row.primary_category]

    return {
        "arxiv_id": row.arxiv_id,
        "title": row.title,
        "title_kor": row.title_kor,
        "summary": row.summary,
        "keywords": _load_json_list(row.keywords_json),
        "field": row.field,
        "fields": fields,
        "matched_categories": matched_categories,
        "arxiv_categories": arxiv_categories,
        "primary_category": row.primary_category,
        "link": row.link,
        "authors": _load_json_list(row.authors_json),
        "listing_date": row.listing_date,
        "published_at": row.published_at,
        "updated_at": row.updated_at,
    }


def _date_payloads(*, start_date: datetime.date, end_date: datetime.date, counts: dict[str, int]) -> list[dict[str, object]]:
    days = (end_date - start_date).days
    payloads: list[dict[str, object]] = []
    for offset in range(days + 1):
        value = start_date + timedelta(days=offset)
        date_str = value.isoformat()
        count = counts.get(date_str, 0)
        payloads.append(
            {
                "date": date_str,
                "weekday": value.weekday(),
                "is_weekend": value.weekday() >= 5,
                "count": count,
                "has_papers": count > 0,
            }
        )
    return payloads


def _keywords_from_store(settings: Settings) -> list[str]:
    if not settings.keyword_store_path.exists():
        return []
    try:
        data = json.loads(settings.keyword_store_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    keywords = data.get("keywords", []) if isinstance(data, dict) else data
    if not isinstance(keywords, list):
        return []
    return [str(keyword).strip() for keyword in keywords if str(keyword).strip()]


def _category_nav(settings: Settings, papers: list[dict[str, object]]) -> list[dict[str, object]]:
    nav: list[dict[str, object]] = []
    for category in settings.arxiv_categories:
        field = settings.field_by_category.get(category, category)
        count = sum(
            1
            for paper in papers
            if category in paper.get("matched_categories", [])
            or category in paper.get("arxiv_categories", [])
        )
        nav.append(
            {
                "category": category,
                "field": field,
                "label": CATEGORY_LABELS.get(category, category),
                "field_label": FIELD_LABELS.get(field, field.replace("_", " ").title()),
                "count": count,
            }
        )
    return nav


def export_static_site_data(
    *,
    settings: Settings | None = None,
    days: int | None = None,
    output_path: Path | None = None,
    engine: Engine | None = None,
) -> Path:
    settings = settings or load_settings()
    days = days or settings.web_export_days
    output_path = output_path or web_data_path(settings)
    engine = init_db(engine=engine, settings=settings)

    now = datetime.now(ZoneInfo(settings.timezone))
    end_date = now.date()
    start_date = end_date - timedelta(days=max(days - 1, 0))
    start_date_str = start_date.isoformat()

    with Session(engine) as session:
        rows = session.scalars(
            select(PaperRow)
            .where(PaperRow.listing_date >= start_date_str)
            .order_by(PaperRow.listing_date.desc(), PaperRow.modified_at.desc(), PaperRow.id.desc())
        ).all()
        keyword_rows = session.scalars(select(KeywordRow).order_by(KeywordRow.name.asc())).all()
        field_rows = session.scalars(select(FieldRow).order_by(FieldRow.name.asc())).all()

    papers = [_paper_payload(row) for row in rows]
    date_counts: dict[str, int] = {}
    for paper in papers:
        date_str = str(paper["listing_date"])
        date_counts[date_str] = date_counts.get(date_str, 0) + 1

    keyword_names = {row.name: row.usage_count for row in keyword_rows}
    for keyword in _keywords_from_store(settings):
        keyword_names.setdefault(keyword, 0)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": settings.timezone,
        "base_path": settings.web_base_path,
        "api_base_url": settings.web_api_base_url,
        "on_demand_enabled": settings.api_on_demand_enabled,
        "retention_days": settings.database_retention_days,
        "range": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "days": days,
        },
        "categories": _category_nav(settings, papers),
        "fields": [
            {
                "field": row.name,
                "label": FIELD_LABELS.get(row.name, row.name.replace("_", " ").title()),
                "count": row.usage_count,
            }
            for row in field_rows
        ],
        "dates": _date_payloads(start_date=start_date, end_date=end_date, counts=date_counts),
        "keywords": [
            {"name": name, "count": count}
            for name, count in sorted(keyword_names.items(), key=lambda item: item[0].lower())
        ],
        "papers": papers,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[web] exported {len(papers)} papers -> {output_path}")
    return output_path
