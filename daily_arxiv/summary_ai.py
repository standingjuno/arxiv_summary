"""AI summarization and keyword reuse for arXiv papers."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import unicodedata
from typing import Any

from openai import OpenAI

from config.settings import Settings, load_settings, require_openai_api_key
from daily_arxiv.models import RawPaper, SummarizedPaper, SummaryResult


BATCH_ENDPOINT = "/v1/chat/completions"
ACTIVE_BATCH_STATUSES = {"validating", "in_progress", "finalizing", "cancelling"}
READY_BATCH_STATUS = "completed"
RETRYABLE_TERMINAL_BATCH_STATUSES = {
    "failed",
    "expired",
    "cancelled",
    "completed_with_errors",
}

SYSTEM_PROMPT = """\
You summarize arXiv robotics and machine learning papers for a Korean researcher.
Return only one valid JSON object.

Required JSON schema:
{
  "title_kor": "Korean translation of the original title",
  "summary": "One concise Korean sentence summarizing the abstract",
  "keywords": ["keyword1", "keyword2", "keyword3"]
}

Rules:
- Keep the original title out of title_kor.
- summary must be exactly one Korean sentence.
- keywords must be one to five concise technical keywords, preferably English canonical labels or acronyms.
- Fewer than five keywords are acceptable when only a smaller set is meaningful after reuse and canonicalization.
- Reuse an existing keyword when it has the same or a very similar meaning.
- Create a new keyword only when no existing keyword is suitable.
- Prefer canonical acronyms and short labels when they are listed, such as SLAM, LiDAR, LIO, LO, LLM, VLM, VLN, VLA, RL, and IL.
- Prefer concise labels such as Diffusion and Navigation over longer phrases when the meaning is equivalent.
- Do not include both a long form and its acronym in the same keyword list.
"""


class SummaryBatchPending(RuntimeError):
    """Raised when a summary batch exists but results are not ready yet."""

    def __init__(self, *, date_str: str, batch_id: str, status: str) -> None:
        self.date_str = date_str
        self.batch_id = batch_id
        self.status = status
        super().__init__(
            f"summary batch is not ready date={date_str} batch_id={batch_id} status={status}"
        )


def summary_output_path(settings: Settings, date_str: str) -> Path:
    return settings.summary_dir / f"arxiv_summaries_{date_str}.json"


def batch_input_path(settings: Settings, date_str: str) -> Path:
    return settings.batch_dir / f"summary_batch_{date_str}.input.jsonl"


def batch_state_path(settings: Settings, date_str: str) -> Path:
    return settings.batch_dir / f"summary_batch_{date_str}.state.json"


def batch_output_path(settings: Settings, date_str: str) -> Path:
    return settings.batch_dir / f"summary_batch_{date_str}.output.jsonl"


def batch_error_path(settings: Settings, date_str: str) -> Path:
    return settings.batch_dir / f"summary_batch_{date_str}.errors.jsonl"


def normalize_keyword(keyword: str) -> str:
    normalized = unicodedata.normalize("NFKC", keyword).casefold()
    return "".join(char for char in normalized if char.isalnum())


class KeywordStore:
    """JSON-backed keyword registry used before the database layer exists."""

    def __init__(
        self,
        path: Path,
        *,
        seed_keywords: tuple[str, ...] = (),
        aliases: dict[str, str] | None = None,
        keywords: list[str] | None = None,
    ) -> None:
        self.path = path
        self._keywords: dict[str, str] = {}
        self._aliases = {
            normalize_keyword(alias): canonical.strip()
            for alias, canonical in (aliases or {}).items()
            if alias.strip() and canonical.strip()
        }
        for keyword in self._aliases.values():
            self.add(keyword)
        for keyword in seed_keywords:
            self.add(keyword)
        for keyword in keywords or []:
            self.add(keyword)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        seed_keywords: tuple[str, ...] = (),
        aliases: dict[str, str] | None = None,
    ) -> "KeywordStore":
        if not path.exists():
            store = cls(path, seed_keywords=seed_keywords, aliases=aliases)
            store.save()
            return store

        data = json.loads(path.read_text(encoding="utf-8"))
        raw_keywords = data.get("keywords", []) if isinstance(data, dict) else data
        if not isinstance(raw_keywords, list):
            raw_keywords = []
        store = cls(
            path,
            seed_keywords=seed_keywords,
            aliases=aliases,
            keywords=[str(keyword) for keyword in raw_keywords],
        )
        store.save()
        return store

    def _preferred_keyword(self, keyword: str) -> str:
        keyword = keyword.strip()
        for _ in range(3):
            normalized = normalize_keyword(keyword)
            mapped = self._aliases.get(normalized)
            if not mapped or mapped == keyword:
                break
            keyword = mapped.strip()
        return keyword

    def add(self, keyword: str) -> str | None:
        keyword = self._preferred_keyword(keyword)
        normalized = normalize_keyword(keyword)
        if not keyword or not normalized:
            return None
        if normalized not in self._keywords:
            self._keywords[normalized] = keyword
        return self._keywords[normalized]

    def canonicalize(self, keyword: str) -> str | None:
        keyword = self._preferred_keyword(keyword)
        normalized = normalize_keyword(keyword)
        if not normalized:
            return None
        return self._keywords.get(normalized) or self.add(keyword)

    def update(self, keywords: list[str]) -> list[str]:
        canonical: list[str] = []
        for keyword in keywords:
            value = self.canonicalize(keyword)
            if value and value not in canonical:
                canonical.append(value)
        self.save()
        return canonical

    def prompt_keywords(self, limit: int = 200) -> list[str]:
        return list(self._keywords.values())[:limit]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "keywords": sorted(self._keywords.values(), key=str.lower),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_user_prompt(
    paper: RawPaper,
    known_keywords: list[str],
    keyword_aliases: dict[str, str],
) -> str:
    known = ", ".join(known_keywords) if known_keywords else "(none)"
    aliases = ", ".join(
        f"{alias} -> {canonical}"
        for alias, canonical in sorted(keyword_aliases.items(), key=lambda item: item[0].lower())[:100]
    )
    alias_text = aliases if aliases else "(none)"
    return (
        f"Fields from watched arXiv categories: {', '.join(paper.fields)}\n"
        f"Primary category: {paper.primary_category}\n"
        f"Matched watched categories: {', '.join(paper.matched_categories)}\n"
        f"All arXiv categories on paper: {', '.join(paper.arxiv_categories)}\n"
        f"Known reusable canonical keywords: {known}\n"
        f"Canonical alias rules: {alias_text}\n\n"
        f"Original title: {paper.title}\n\n"
        f"Abstract: {paper.abstract[:4000]}"
    )


def _load_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("AI response must be a JSON object.")
    return data


def _summarized_paper_from_json_content(
    paper: RawPaper,
    content: str,
    *,
    keyword_store: KeywordStore,
) -> SummarizedPaper:
    result = SummaryResult.model_validate(_load_json_object(content))
    keywords = keyword_store.update(result.keywords)
    return SummarizedPaper(
        **paper.model_dump(mode="json"),
        title_kor=result.title_kor,
        summary=result.summary,
        keywords=keywords,
    )


def summarize_paper_openai(
    paper: RawPaper,
    *,
    settings: Settings,
    keyword_store: KeywordStore,
    client: OpenAI | None = None,
) -> SummarizedPaper:
    api_key = require_openai_api_key(settings)
    client = client or OpenAI(api_key=api_key)
    known_keywords = keyword_store.prompt_keywords()
    user_prompt = build_user_prompt(paper, known_keywords, settings.keyword_aliases)
    last_error: Exception | None = None

    for attempt in range(1, settings.summary_max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            return _summarized_paper_from_json_content(
                paper,
                content,
                keyword_store=keyword_store,
            )
        except Exception as exc:
            last_error = exc
            if attempt >= settings.summary_max_retries:
                break
            sleep_seconds = settings.summary_sleep_seconds * attempt
            print(
                f"[summary] retry arxiv_id={paper.arxiv_id} "
                f"attempt={attempt}/{settings.summary_max_retries} error={exc}"
            )
            time.sleep(sleep_seconds)

    raise RuntimeError(f"Failed to summarize {paper.arxiv_id}: {last_error}") from last_error


def summarize_paper(
    paper: RawPaper,
    *,
    settings: Settings,
    keyword_store: KeywordStore,
    client: OpenAI | None = None,
) -> SummarizedPaper:
    if settings.ai_provider != "openai":
        raise NotImplementedError(f"Unsupported AI_PROVIDER: {settings.ai_provider}")
    return summarize_paper_openai(
        paper,
        settings=settings,
        keyword_store=keyword_store,
        client=client,
    )


def _load_existing_summaries(
    *,
    settings: Settings,
    date_str: str,
    resume: bool,
) -> dict[str, SummarizedPaper]:
    if not resume or not summary_output_path(settings, date_str).exists():
        return {}
    return {
        paper.arxiv_id: paper
        for paper in load_summarized_papers(settings=settings, date_str=date_str)
    }


def _merge_summaries_in_paper_order(
    papers: list[RawPaper],
    existing_by_id: dict[str, SummarizedPaper],
    updated_papers: list[SummarizedPaper],
) -> list[SummarizedPaper]:
    merged_by_id = dict(existing_by_id)
    merged_by_id.update({paper.arxiv_id: paper for paper in updated_papers})
    return [
        merged_by_id[paper.arxiv_id]
        for paper in papers
        if paper.arxiv_id in merged_by_id
    ]


def _keyword_store(settings: Settings) -> KeywordStore:
    return KeywordStore.load(
        settings.keyword_store_path,
        seed_keywords=settings.keyword_seed_keywords,
        aliases=settings.keyword_aliases,
    )


def _batch_custom_id(index: int, paper: RawPaper) -> str:
    normalized_arxiv_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", paper.arxiv_id)
    return f"paper-{index}-{normalized_arxiv_id}"


def _batch_request(
    *,
    custom_id: str,
    paper: RawPaper,
    settings: Settings,
    keyword_store: KeywordStore,
) -> dict[str, Any]:
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": BATCH_ENDPOINT,
        "body": {
            "model": settings.openai_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_user_prompt(
                        paper,
                        keyword_store.prompt_keywords(),
                        settings.keyword_aliases,
                    ),
                },
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        },
    }


def _openai_object_to_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return value
    return json.loads(json.dumps(value, default=str))


def _write_batch_state(settings: Settings, date_str: str, state: dict[str, Any]) -> Path:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = batch_state_path(settings, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_batch_state(settings: Settings, date_str: str) -> dict[str, Any] | None:
    path = batch_state_path(settings, date_str)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def _update_batch_state_from_batch(
    *,
    settings: Settings,
    date_str: str,
    state: dict[str, Any],
    batch: Any,
) -> dict[str, Any]:
    batch_data = _openai_object_to_dict(batch)
    state.update(
        {
            "batch": batch_data,
            "batch_id": batch_data.get("id") or state.get("batch_id"),
            "status": batch_data.get("status") or state.get("status"),
            "output_file_id": batch_data.get("output_file_id"),
            "error_file_id": batch_data.get("error_file_id"),
        }
    )
    _write_batch_state(settings, date_str, state)
    return state


def _file_content_text(client: OpenAI, file_id: str) -> str:
    file_response = client.files.content(file_id)
    if hasattr(file_response, "text"):
        return str(file_response.text)
    if hasattr(file_response, "content"):
        content = file_response.content
        return content.decode("utf-8") if isinstance(content, bytes) else str(content)
    if hasattr(file_response, "read"):
        content = file_response.read()
        return content.decode("utf-8") if isinstance(content, bytes) else str(content)
    return str(file_response)


def _write_batch_content(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _chat_content_from_batch_record(record: dict[str, Any]) -> str:
    if record.get("error"):
        raise RuntimeError(str(record["error"]))

    response = record.get("response") or {}
    status_code = response.get("status_code")
    if status_code != 200:
        raise RuntimeError(f"Batch request failed status_code={status_code} response={response}")

    body = response.get("body") or {}
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("Batch response has no choices.")

    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if not content:
        raise RuntimeError("Batch response message content is empty.")
    return str(content)


def _process_batch_output(
    *,
    settings: Settings,
    date_str: str,
    state: dict[str, Any],
    raw_papers: list[RawPaper],
    existing_by_id: dict[str, SummarizedPaper],
    save: bool,
    client: OpenAI,
) -> list[SummarizedPaper]:
    output_file_id = str(state.get("output_file_id") or "")
    if not output_file_id:
        raise RuntimeError(f"Completed batch has no output_file_id date={date_str}")

    output_content = _file_content_text(client, output_file_id)
    _write_batch_content(batch_output_path(settings, date_str), output_content)

    error_file_id = state.get("error_file_id")
    if error_file_id:
        error_content = _file_content_text(client, str(error_file_id))
        _write_batch_content(batch_error_path(settings, date_str), error_content)

    custom_id_to_arxiv_id = {
        str(custom_id): str(arxiv_id)
        for custom_id, arxiv_id in (state.get("custom_id_to_arxiv_id") or {}).items()
    }
    raw_by_id = {paper.arxiv_id: paper for paper in raw_papers}
    keyword_store = _keyword_store(settings)
    summarized_by_id = dict(existing_by_id)
    failed_records: list[str] = []

    for line_number, line in enumerate(output_content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            custom_id = str(record.get("custom_id") or "")
            arxiv_id = custom_id_to_arxiv_id.get(custom_id)
            if not arxiv_id:
                raise RuntimeError(f"Unknown custom_id={custom_id}")
            paper = raw_by_id.get(arxiv_id)
            if paper is None:
                raise RuntimeError(f"Raw paper not found for arxiv_id={arxiv_id}")

            content = _chat_content_from_batch_record(record)
            summarized_by_id[arxiv_id] = _summarized_paper_from_json_content(
                paper,
                content,
                keyword_store=keyword_store,
            )
        except Exception as exc:
            failed_records.append(f"line={line_number} error={exc}")

    requested_ids = [str(arxiv_id) for arxiv_id in state.get("arxiv_ids", [])]
    missing_ids = [arxiv_id for arxiv_id in requested_ids if arxiv_id not in summarized_by_id]
    if missing_ids:
        failed_records.append(f"missing_results={missing_ids}")

    selected_id_set = {paper.arxiv_id for paper in raw_papers}
    summarized = [
        summarized_by_id[paper.arxiv_id]
        for paper in raw_papers
        if paper.arxiv_id in summarized_by_id and paper.arxiv_id in selected_id_set
    ]

    if save:
        save_summarized_papers(summarized, settings=settings, date_str=date_str)

    if failed_records:
        state["status"] = "completed_with_errors"
        state["result_saved"] = False
        state["failed_records"] = failed_records
        _write_batch_state(settings, date_str, state)
        raise RuntimeError(
            f"Batch completed with {len(failed_records)} failed summary records. "
            f"See {batch_state_path(settings, date_str)}"
        )

    state["status"] = "completed"
    state["result_saved"] = True
    state["completed_at_local"] = datetime.now(timezone.utc).isoformat()
    _write_batch_state(settings, date_str, state)
    print(f"[summary-batch] completed date={date_str} papers={len(summarized)}")
    return summarized


def _create_summary_batch(
    *,
    settings: Settings,
    date_str: str,
    papers: list[RawPaper],
    client: OpenAI,
) -> None:
    keyword_store = _keyword_store(settings)
    custom_id_to_arxiv_id: dict[str, str] = {}
    input_path = batch_input_path(settings, date_str)
    input_path.parent.mkdir(parents=True, exist_ok=True)

    with input_path.open("w", encoding="utf-8") as file:
        for index, paper in enumerate(papers, start=1):
            custom_id = _batch_custom_id(index, paper)
            custom_id_to_arxiv_id[custom_id] = paper.arxiv_id
            request = _batch_request(
                custom_id=custom_id,
                paper=paper,
                settings=settings,
                keyword_store=keyword_store,
            )
            file.write(json.dumps(request, ensure_ascii=False) + "\n")

    with input_path.open("rb") as file:
        batch_input_file = client.files.create(file=file, purpose="batch")

    batch = client.batches.create(
        input_file_id=batch_input_file.id,
        endpoint=BATCH_ENDPOINT,
        completion_window=settings.openai_batch_completion_window,
        metadata={
            "app": "arxiv_summary",
            "date": date_str,
            "model": settings.openai_model,
        },
    )
    batch_data = _openai_object_to_dict(batch)
    state = {
        "date": date_str,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.openai_model,
        "endpoint": BATCH_ENDPOINT,
        "completion_window": settings.openai_batch_completion_window,
        "input_path": str(input_path),
        "input_file_id": batch_input_file.id,
        "batch_id": batch_data.get("id"),
        "status": batch_data.get("status"),
        "arxiv_ids": [paper.arxiv_id for paper in papers],
        "custom_id_to_arxiv_id": custom_id_to_arxiv_id,
        "batch": batch_data,
    }
    _write_batch_state(settings, date_str, state)
    print(
        f"[summary-batch] submitted date={date_str} "
        f"batch_id={state['batch_id']} requests={len(papers)}"
    )


def _retrieve_batch_with_optional_wait(
    *,
    client: OpenAI,
    batch_id: str,
    settings: Settings,
) -> Any:
    batch = client.batches.retrieve(batch_id)
    timeout = settings.openai_batch_wait_timeout_seconds
    if timeout <= 0:
        return batch

    deadline = time.monotonic() + timeout
    while getattr(batch, "status", None) in ACTIVE_BATCH_STATUSES and time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        sleep_seconds = min(settings.openai_batch_poll_interval_seconds, remaining)
        if sleep_seconds <= 0:
            break
        print(
            f"[summary-batch] waiting batch_id={batch_id} "
            f"status={getattr(batch, 'status', 'unknown')} sleep={sleep_seconds:.0f}s"
        )
        time.sleep(sleep_seconds)
        batch = client.batches.retrieve(batch_id)
    return batch


def summarize_papers_batch_openai(
    papers: list[RawPaper],
    *,
    date_str: str,
    settings: Settings,
    limit: int | None = None,
    save: bool = True,
    resume: bool = True,
) -> list[SummarizedPaper]:
    existing_by_id = _load_existing_summaries(
        settings=settings,
        date_str=date_str,
        resume=resume,
    )
    selected_papers = papers[:limit] if limit is not None else papers
    remaining_papers = [
        paper for paper in selected_papers if paper.arxiv_id not in existing_by_id
    ]

    if not remaining_papers:
        summarized = [existing_by_id[paper.arxiv_id] for paper in selected_papers]
        merged = _merge_summaries_in_paper_order(papers, existing_by_id, summarized)
        if save:
            save_summarized_papers(merged, settings=settings, date_str=date_str)
        return merged

    client = OpenAI(api_key=require_openai_api_key(settings))
    state = _load_batch_state(settings, date_str)

    if (
        state
        and not state.get("result_saved")
        and str(state.get("status")) not in RETRYABLE_TERMINAL_BATCH_STATUSES
    ):
        batch_id = str(state.get("batch_id") or "")
        if not batch_id:
            raise RuntimeError(f"Summary batch state is missing batch_id: {batch_state_path(settings, date_str)}")
        batch = _retrieve_batch_with_optional_wait(
            client=client,
            batch_id=batch_id,
            settings=settings,
        )
        state = _update_batch_state_from_batch(
            settings=settings,
            date_str=date_str,
            state=state,
            batch=batch,
        )
        status = str(state.get("status"))
        if status == READY_BATCH_STATUS:
            return _process_batch_output(
                settings=settings,
                date_str=date_str,
                state=state,
                raw_papers=selected_papers,
                existing_by_id=existing_by_id,
                save=save,
                client=client,
            )
        if status in ACTIVE_BATCH_STATUSES:
            raise SummaryBatchPending(
                date_str=date_str,
                batch_id=batch_id,
                status=status,
            )
        raise RuntimeError(
            f"Summary batch ended with status={status} date={date_str} batch_id={batch_id}"
        )

    _create_summary_batch(
        settings=settings,
        date_str=date_str,
        papers=remaining_papers,
        client=client,
    )
    new_state = _load_batch_state(settings, date_str) or {}
    raise SummaryBatchPending(
        date_str=date_str,
        batch_id=str(new_state.get("batch_id") or "unknown"),
        status=str(new_state.get("status") or "submitted"),
    )


def finalize_completed_summary_batches(
    *,
    settings: Settings | None = None,
    save: bool = True,
) -> dict[str, list[SummarizedPaper]]:
    settings = settings or load_settings()
    if settings.ai_provider != "openai" or settings.ai_mode != "batch":
        return {}

    state_paths = sorted(settings.batch_dir.glob("summary_batch_*.state.json"))
    if not state_paths:
        return {}

    client = OpenAI(api_key=require_openai_api_key(settings))
    finalized: dict[str, list[SummarizedPaper]] = {}

    for path in state_paths:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            continue
        if state.get("result_saved"):
            continue
        status = str(state.get("status") or "")
        if status not in ACTIVE_BATCH_STATUSES and status != READY_BATCH_STATUS:
            continue

        date_str = str(state.get("date") or "")
        batch_id = str(state.get("batch_id") or "")
        if not date_str or not batch_id:
            continue

        batch = client.batches.retrieve(batch_id)
        state = _update_batch_state_from_batch(
            settings=settings,
            date_str=date_str,
            state=state,
            batch=batch,
        )
        status = str(state.get("status"))
        if status != READY_BATCH_STATUS:
            print(f"[summary-batch] pending date={date_str} batch_id={batch_id} status={status}")
            continue

        from daily_arxiv.fetch_arxiv import load_raw_papers

        raw_papers = load_raw_papers(settings=settings, date_str=date_str)
        existing_by_id = _load_existing_summaries(
            settings=settings,
            date_str=date_str,
            resume=True,
        )
        finalized[date_str] = _process_batch_output(
            settings=settings,
            date_str=date_str,
            state=state,
            raw_papers=raw_papers,
            existing_by_id=existing_by_id,
            save=save,
            client=client,
        )

    return finalized


def summarize_papers_sync_openai(
    papers: list[RawPaper],
    *,
    date_str: str,
    settings: Settings | None = None,
    limit: int | None = None,
    save: bool = True,
    resume: bool = True,
) -> list[SummarizedPaper]:
    settings = settings or load_settings()
    existing_by_id = _load_existing_summaries(
        settings=settings,
        date_str=date_str,
        resume=resume,
    )

    selected_papers = papers[:limit] if limit is not None else papers
    keyword_store = _keyword_store(settings)
    client = OpenAI(api_key=require_openai_api_key(settings)) if selected_papers else None
    summarized: list[SummarizedPaper] = []

    for index, paper in enumerate(selected_papers, start=1):
        if paper.arxiv_id in existing_by_id:
            summarized.append(existing_by_id[paper.arxiv_id])
            continue

        print(f"[summary] {index}/{len(selected_papers)} arxiv_id={paper.arxiv_id}")
        result = summarize_paper(
            paper,
            settings=settings,
            keyword_store=keyword_store,
            client=client,
        )
        summarized.append(result)

        if save:
            save_summarized_papers(
                _merge_summaries_in_paper_order(papers, existing_by_id, summarized),
                settings=settings,
                date_str=date_str,
            )
        time.sleep(settings.summary_sleep_seconds)

    if save:
        summarized = _merge_summaries_in_paper_order(papers, existing_by_id, summarized)
        save_summarized_papers(summarized, settings=settings, date_str=date_str)
    return summarized


def summarize_papers(
    papers: list[RawPaper],
    *,
    date_str: str,
    settings: Settings | None = None,
    limit: int | None = None,
    save: bool = True,
    resume: bool = True,
) -> list[SummarizedPaper]:
    settings = settings or load_settings()
    if settings.ai_provider != "openai":
        raise NotImplementedError(f"Unsupported AI_PROVIDER: {settings.ai_provider}")
    if settings.ai_mode == "batch":
        return summarize_papers_batch_openai(
            papers,
            date_str=date_str,
            settings=settings,
            limit=limit,
            save=save,
            resume=resume,
        )
    if settings.ai_mode == "sync":
        return summarize_papers_sync_openai(
            papers,
            date_str=date_str,
            settings=settings,
            limit=limit,
            save=save,
            resume=resume,
        )
    raise NotImplementedError(f"Unsupported ai.mode: {settings.ai_mode}")


def save_summarized_papers(
    papers: list[SummarizedPaper],
    *,
    settings: Settings,
    date_str: str,
) -> Path:
    path = summary_output_path(settings, date_str)
    payload = [paper.model_dump(mode="json") for paper in papers]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[summary] saved {len(papers)} summarized papers -> {path}")
    return path


def load_summarized_papers(*, settings: Settings, date_str: str) -> list[SummarizedPaper]:
    path = summary_output_path(settings, date_str)
    if not path.exists():
        raise FileNotFoundError(f"Summary file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    papers = [SummarizedPaper.model_validate(item) for item in data]
    print(f"[summary] loaded {len(papers)} summarized papers <- {path}")
    return papers
