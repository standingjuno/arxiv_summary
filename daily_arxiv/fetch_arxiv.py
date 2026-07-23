"""Fetch arXiv papers for one or more watched categories."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import arxiv

from config.settings import Settings, load_settings
from daily_arxiv.models import RawPaper


_LAST_ARXIV_REQUEST_AT = 0.0


def raw_output_path(settings: Settings, date_str: str) -> Path:
    return settings.raw_dir / f"arxiv_raw_{date_str}.json"


def previous_business_day(value: date) -> date:
    value = value - timedelta(days=1)
    while value.weekday() >= 5:
        value = value - timedelta(days=1)
    return value


def submission_window(listing_date: date) -> tuple[datetime, datetime]:
    """Return the arXiv submittedDate window for an announcement listing date."""
    eastern = ZoneInfo("America/New_York")
    end_day = previous_business_day(listing_date)
    start_day = previous_business_day(end_day)
    start = datetime.combine(start_day, datetime.min.time().replace(hour=14), tzinfo=eastern)
    end = datetime.combine(end_day, datetime.min.time().replace(hour=14), tzinfo=eastern)
    return start, end


def build_submitted_date_query(category: str, listing_date: date) -> str:
    start_et, end_et = submission_window(listing_date)
    utc = ZoneInfo("UTC")
    start_str = start_et.astimezone(utc).strftime("%Y%m%d%H%M")
    end_str = end_et.astimezone(utc).strftime("%Y%m%d%H%M")
    return f"cat:{category} AND submittedDate:[{start_str} TO {end_str}]"


def build_submitted_date_range_query(category: str, listing_dates: list[date]) -> str:
    windows = [submission_window(listing_date) for listing_date in listing_dates]
    utc = ZoneInfo("UTC")
    start_utc = min(start_et.astimezone(utc) for start_et, _ in windows)
    end_utc = max(end_et.astimezone(utc) for _, end_et in windows)
    start_str = start_utc.strftime("%Y%m%d%H%M")
    end_str = end_utc.strftime("%Y%m%d%H%M")
    return f"cat:{category} AND submittedDate:[{start_str} TO {end_str}]"


def _wait_for_arxiv_slot(settings: Settings) -> None:
    global _LAST_ARXIV_REQUEST_AT

    interval = max(settings.arxiv_min_request_interval, settings.arxiv_delay_seconds)
    elapsed = time.monotonic() - _LAST_ARXIV_REQUEST_AT
    if _LAST_ARXIV_REQUEST_AT and elapsed < interval:
        sleep_seconds = interval - elapsed
        print(f"[fetch] respecting arXiv rate limit: sleep={sleep_seconds:.1f}s")
        time.sleep(sleep_seconds)
    _LAST_ARXIV_REQUEST_AT = time.monotonic()


def _is_rate_limit_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "429" in message or "rate exceeded" in message or "too many requests" in message


def _run_arxiv_search(
    *,
    search: arxiv.Search,
    settings: Settings,
) -> list[arxiv.Result]:
    delays = settings.arxiv_retry_delays
    attempts = len(delays) + 1

    for attempt in range(1, attempts + 1):
        client = arxiv.Client(
            page_size=settings.arxiv_page_size,
            delay_seconds=settings.arxiv_delay_seconds,
            num_retries=0,
        )

        try:
            _wait_for_arxiv_slot(settings)
            return list(client.results(search))
        except Exception as exc:
            if not _is_rate_limit_error(exc) or attempt >= attempts:
                raise

            delay = delays[attempt - 1]
            print(
                f"[fetch] arXiv rate limited attempt={attempt}/{attempts}; "
                f"sleep={delay:.0f}s error={exc}"
            )
            time.sleep(delay)

    raise RuntimeError("unreachable arXiv retry state")


def _result_categories(result: arxiv.Result) -> list[str]:
    categories = getattr(result, "categories", None) or []
    cleaned: list[str] = []
    for category in categories:
        category = str(category).strip()
        if category and category not in cleaned:
            cleaned.append(category)
    if result.primary_category and result.primary_category not in cleaned:
        cleaned.insert(0, result.primary_category)
    return cleaned


def _matched_categories(
    result: arxiv.Result,
    *,
    selected_categories: tuple[str, ...],
    fallback_category: str,
) -> list[str]:
    result_categories = _result_categories(result)
    matched = [category for category in selected_categories if category in result_categories]
    if matched:
        return matched
    return [fallback_category]


def _fields_for_categories(categories: list[str], settings: Settings) -> list[str]:
    fields: list[str] = []
    for category in categories:
        field = settings.field_by_category.get(category, category)
        if field not in fields:
            fields.append(field)
    return fields


def _raw_paper_from_result(
    result: arxiv.Result,
    *,
    matched_categories: list[str],
    fields: list[str],
    listing_date: str,
) -> RawPaper:
    field = fields[0] if fields else result.primary_category
    return RawPaper(
        arxiv_id=result.entry_id.rsplit("/", 1)[-1],
        title=result.title.strip().replace("\n", " "),
        authors=[author.name for author in result.authors],
        link=result.entry_id,
        abstract=result.summary.strip().replace("\n", " "),
        primary_category=result.primary_category,
        arxiv_categories=_result_categories(result),
        matched_categories=matched_categories,
        field=field,
        fields=fields or [field],
        listing_date=listing_date,
        published_at=result.published.isoformat() if result.published else None,
        updated_at=result.updated.isoformat() if result.updated else None,
    )


def _merge_raw_papers(existing: RawPaper, incoming: RawPaper) -> RawPaper:
    arxiv_categories = [
        *existing.arxiv_categories,
        *[
            category
            for category in incoming.arxiv_categories
            if category not in existing.arxiv_categories
        ],
    ]
    matched_categories = [
        *existing.matched_categories,
        *[
            category
            for category in incoming.matched_categories
            if category not in existing.matched_categories
        ],
    ]
    fields = [
        *existing.fields,
        *[field for field in incoming.fields if field not in existing.fields],
    ]
    return existing.model_copy(
        update={
            "arxiv_categories": arxiv_categories,
            "matched_categories": matched_categories,
            "field": fields[0] if fields else existing.field,
            "fields": fields,
        }
    )


def fetch_category_papers(
    *,
    date_str: str,
    category: str,
    selected_categories: tuple[str, ...],
    settings: Settings,
) -> list[RawPaper]:
    listing_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    if listing_date.weekday() >= 5:
        print(f"[fetch] {date_str} is a weekend. arXiv has no regular listing.")
        return []

    query = build_submitted_date_query(category, listing_date)
    search = arxiv.Search(
        query=query,
        max_results=settings.arxiv_max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    papers: list[RawPaper] = []
    print(f"[fetch] category={category} query={query}")

    for result in _run_arxiv_search(search=search, settings=settings):
        matched = _matched_categories(
            result,
            selected_categories=selected_categories,
            fallback_category=category,
        )
        fields = _fields_for_categories(matched, settings)

        papers.append(
            _raw_paper_from_result(
                result,
                matched_categories=matched,
                fields=fields,
                listing_date=date_str,
            )
        )

    print(f"[fetch] category={category} matched={len(papers)}")
    return papers


def _date_range(start_date: date, end_date: date) -> list[date]:
    if start_date > end_date:
        raise ValueError("start_date must be before or equal to end_date")
    days = (end_date - start_date).days
    return [start_date + timedelta(days=i) for i in range(days + 1)]


def _find_listing_date(
    result: arxiv.Result,
    windows: dict[str, tuple[datetime, datetime]],
) -> str | None:
    published = result.published
    if published is None:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=ZoneInfo("UTC"))
    published_utc = published.astimezone(ZoneInfo("UTC"))

    for date_str, (start_utc, end_utc) in windows.items():
        if start_utc <= published_utc <= end_utc:
            return date_str
    return None


def fetch_arxiv_papers_range(
    *,
    start_date_str: str,
    end_date_str: str,
    categories: tuple[str, ...] | None = None,
    settings: Settings | None = None,
    save: bool = True,
) -> dict[str, list[RawPaper]]:
    """Fetch a date range with one broad arXiv query per category.

    This is safer for backfills than making date-by-date API calls because it
    dramatically reduces request count and localizes the date bucketing.
    """
    settings = settings or load_settings()
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    all_dates = _date_range(start_date, end_date)
    listing_dates = [value for value in all_dates if value.weekday() < 5]
    papers_by_date: dict[str, dict[str, RawPaper]] = {value.isoformat(): {} for value in all_dates}

    if not listing_dates:
        if save:
            for value in all_dates:
                save_raw_papers([], settings=settings, date_str=value.isoformat())
        return {date_str: [] for date_str in papers_by_date}

    utc = ZoneInfo("UTC")
    windows = {
        value.isoformat(): tuple(item.astimezone(utc) for item in submission_window(value))
        for value in listing_dates
    }

    selected_categories = categories or settings.arxiv_categories
    for category in selected_categories:
        query = build_submitted_date_range_query(category, listing_dates)
        search = arxiv.Search(
            query=query,
            max_results=settings.arxiv_max_results * len(listing_dates),
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Ascending,
        )
        outside_window = 0
        matched_count = 0
        print(f"[fetch-range] category={category} query={query}")

        for result in _run_arxiv_search(search=search, settings=settings):
            listing_date = _find_listing_date(result, windows)
            if listing_date is None:
                outside_window += 1
                continue

            matched = _matched_categories(
                result,
                selected_categories=selected_categories,
                fallback_category=category,
            )
            fields = _fields_for_categories(matched, settings)
            matched_count += 1
            paper = _raw_paper_from_result(
                result,
                matched_categories=matched,
                fields=fields,
                listing_date=listing_date,
            )
            existing = papers_by_date[listing_date].get(paper.arxiv_id)
            papers_by_date[listing_date][paper.arxiv_id] = (
                _merge_raw_papers(existing, paper) if existing else paper
            )

        print(
            f"[fetch-range] category={category} matched={matched_count} "
            f"outside_window={outside_window}"
        )

    result_by_date = {
        date_str: list(papers_by_id.values())
        for date_str, papers_by_id in papers_by_date.items()
    }
    if save:
        for date_str, papers in result_by_date.items():
            save_raw_papers(papers, settings=settings, date_str=date_str)
    return result_by_date


def fetch_arxiv_papers(
    *,
    date_str: str | None = None,
    categories: tuple[str, ...] | None = None,
    settings: Settings | None = None,
    save: bool = True,
) -> list[RawPaper]:
    settings = settings or load_settings()
    if date_str is None:
        date_str = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")

    selected_categories = categories or settings.arxiv_categories
    papers_by_id: dict[str, RawPaper] = {}

    for category in selected_categories:
        for paper in fetch_category_papers(
            date_str=date_str,
            category=category,
            selected_categories=selected_categories,
            settings=settings,
        ):
            existing = papers_by_id.get(paper.arxiv_id)
            papers_by_id[paper.arxiv_id] = _merge_raw_papers(existing, paper) if existing else paper

    papers = list(papers_by_id.values())
    if save:
        save_raw_papers(papers, settings=settings, date_str=date_str)
    return papers


def save_raw_papers(papers: list[RawPaper], *, settings: Settings, date_str: str) -> Path:
    path = raw_output_path(settings, date_str)
    payload = [paper.model_dump(mode="json") for paper in papers]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[fetch] saved {len(papers)} raw papers -> {path}")
    return path


def load_raw_papers(*, settings: Settings, date_str: str) -> list[RawPaper]:
    path = raw_output_path(settings, date_str)
    if not path.exists():
        raise FileNotFoundError(f"Raw paper file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    papers = [RawPaper.model_validate(item) for item in data]
    print(f"[fetch] loaded {len(papers)} raw papers <- {path}")
    return papers
