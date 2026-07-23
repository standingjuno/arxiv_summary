import argparse
from datetime import date, datetime, timedelta
import sys
from zoneinfo import ZoneInfo

from config.settings import Settings, load_settings
from daily_arxiv.database import cleanup_old_papers, init_db, save_summarized_papers_to_db
from daily_arxiv.fetch_arxiv import fetch_arxiv_papers, fetch_arxiv_papers_range, load_raw_papers
from daily_arxiv.models import RawPaper, SummarizedPaper
from daily_arxiv.run_state import (
    PipelineAlreadyRunning,
    RunSpec,
    clear_failed_run,
    load_failed_run_specs,
    pipeline_lock,
    record_failed_run,
)
from daily_arxiv.summary_ai import (
    SummaryBatchPending,
    finalize_completed_summary_batches,
    load_summarized_papers,
    summarize_papers,
)
from daily_arxiv.static_export import export_static_site_data


STEPS = ("fetch", "summary", "store", "export-web", "cleanup-db", "init-db", "all")


def parse_categories(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    categories = tuple(item.strip() for item in value.split(",") if item.strip())
    return categories or None


def iter_dates(start_date_str: str, end_date_str: str) -> list[str]:
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    if start_date > end_date:
        raise ValueError("--start-date must be before or equal to --end-date")
    return [
        (start_date + timedelta(days=i)).isoformat()
        for i in range((end_date - start_date).days + 1)
    ]


def run_pipeline(
    *,
    date_str: str,
    step: str = "all",
    categories: tuple[str, ...] | None = None,
    limit: int | None = None,
    settings: Settings | None = None,
) -> bool:
    settings = settings or load_settings()
    raw_papers: list[RawPaper] | None = None
    summarized_papers: list[SummarizedPaper] | None = None

    print(f"=== arxiv_summary | date={date_str} | step={step} ===")
    print(f"[config] path={settings.config_path or 'built-in defaults'}")
    print(f"[config] categories={categories or settings.arxiv_categories}")
    print(f"[config] database_url={settings.database_url}")

    if step == "init-db":
        init_db(settings=settings)
        return True

    if step == "export-web":
        export_static_site_data(settings=settings)
        return True

    if step == "cleanup-db":
        cleanup_old_papers(settings=settings)
        return True

    if step in {"fetch", "all"}:
        raw_papers = fetch_arxiv_papers(
            date_str=date_str,
            categories=categories,
            settings=settings,
            save=True,
        )

    if step in {"summary", "all"}:
        if raw_papers is None:
            raw_papers = load_raw_papers(settings=settings, date_str=date_str)
        try:
            summarized_papers = summarize_papers(
                raw_papers,
                date_str=date_str,
                settings=settings,
                limit=limit,
                save=True,
                resume=True,
            )
        except SummaryBatchPending as exc:
            print(f"[summary-batch] {exc}")
            print("[summary-batch] store step will run after the batch is completed.")
            return False

    if step in {"store", "all"}:
        if summarized_papers is None:
            summarized_papers = load_summarized_papers(settings=settings, date_str=date_str)
        save_summarized_papers_to_db(summarized_papers, settings=settings)
        if settings.web_auto_export:
            export_static_site_data(settings=settings)

    print("=== done ===")
    return True


def finalize_ready_summary_batches(settings: Settings, *, store: bool) -> None:
    finalized = finalize_completed_summary_batches(settings=settings, save=True)
    if not finalized:
        return

    for date_str, papers in finalized.items():
        if store:
            print(f"[summary-batch] storing finalized batch date={date_str}")
            save_summarized_papers_to_db(papers, settings=settings)
            if settings.web_auto_export:
                export_static_site_data(settings=settings)


def run_date_range_pipeline(
    *,
    start_date_str: str,
    end_date_str: str,
    step: str = "all",
    categories: tuple[str, ...] | None = None,
    limit: int | None = None,
    settings: Settings | None = None,
) -> bool:
    settings = settings or load_settings()
    date_strings = iter_dates(start_date_str, end_date_str)

    print(f"=== arxiv_summary range | start={start_date_str} | end={end_date_str} | step={step} ===")
    print(f"[config] path={settings.config_path or 'built-in defaults'}")
    print(f"[config] categories={categories or settings.arxiv_categories}")
    print(f"[config] database_url={settings.database_url}")

    if step == "init-db":
        init_db(settings=settings)
        return True

    if step == "export-web":
        export_static_site_data(settings=settings)
        return True

    if step == "cleanup-db":
        cleanup_old_papers(settings=settings)
        return True

    all_ready = True

    if step in {"fetch", "all"}:
        fetch_arxiv_papers_range(
            start_date_str=start_date_str,
            end_date_str=end_date_str,
            categories=categories,
            settings=settings,
            save=True,
        )

    if step in {"summary", "store", "all"}:
        for date_str in date_strings:
            current_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if current_date.weekday() >= 5:
                print(f"[range] skip weekend date={date_str}")
                continue
            if step == "summary":
                ready = run_pipeline(
                    date_str=date_str,
                    step="summary",
                    categories=categories,
                    limit=limit,
                    settings=settings,
                )
                all_ready = all_ready and ready
            elif step == "store":
                ready = run_pipeline(
                    date_str=date_str,
                    step="store",
                    categories=categories,
                    limit=limit,
                    settings=settings,
                )
                all_ready = all_ready and ready
            else:
                summary_ready = run_pipeline(
                    date_str=date_str,
                    step="summary",
                    categories=categories,
                    limit=limit,
                    settings=settings,
                )
                all_ready = all_ready and summary_ready
                if summary_ready:
                    store_ready = run_pipeline(
                        date_str=date_str,
                        step="store",
                        categories=categories,
                        limit=limit,
                        settings=settings,
                    )
                    all_ready = all_ready and store_ready

    print("=== range done ===")
    return all_ready


def retry_failed_runs(settings: Settings) -> bool:
    failed_specs = load_failed_run_specs(settings)
    if not failed_specs:
        print("[state] no failed runs to retry")
        return True

    print(f"[state] retrying failed runs count={len(failed_specs)}")
    all_ok = True
    for spec in failed_specs:
        try:
            ready = run_pipeline(
                date_str=spec.date_str,
                step=spec.step,
                categories=spec.categories,
                limit=spec.limit,
                settings=settings,
            )
            if ready:
                clear_failed_run(settings, spec)
        except Exception as exc:
            all_ok = False
            record_failed_run(settings, spec, exc)
            print(f"[state] failed retry date={spec.date_str} step={spec.step} error={exc}")
    return all_ok


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch arXiv papers, summarize them with AI, and store them in a database.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a TOML config file. Defaults to config/settings.toml.",
    )
    parser.add_argument(
        "--date",
        "-d",
        default=None,
        metavar="YYYY-MM-DD",
        help="Listing date to process. Defaults to today in TIMEZONE.",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Start listing date for a range backfill.",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="End listing date for a range backfill.",
    )
    parser.add_argument(
        "--step",
        "-s",
        default="all",
        choices=STEPS,
        help="Pipeline step to run.",
    )
    parser.add_argument(
        "--categories",
        default=None,
        help="Comma-separated arXiv categories. Example: cs.RO,cs.LG,stat.ML",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit the number of papers passed to the AI summarizer.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry failed runs before the requested job.",
    )
    parser.add_argument(
        "--retry-failed-only",
        action="store_true",
        help="Retry failed runs and exit.",
    )
    args = parser.parse_args()
    settings = load_settings(args.config)
    date_str = args.date or datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
    categories = parse_categories(args.categories)

    if bool(args.start_date) != bool(args.end_date):
        parser.error("--start-date and --end-date must be provided together.")

    current_spec = RunSpec(
        date_str=date_str,
        step=args.step,
        categories=categories,
        limit=args.limit,
    )

    exit_code = 0
    try:
        with pipeline_lock(settings):
            if (
                settings.ai_provider == "openai"
                and settings.ai_mode == "batch"
                and (args.step in {"summary", "store", "all"} or args.retry_failed or args.retry_failed_only)
            ):
                finalize_ready_summary_batches(
                    settings,
                    store=args.step in {"store", "all"} or args.retry_failed or args.retry_failed_only,
                )

            if args.retry_failed or args.retry_failed_only:
                if not retry_failed_runs(settings):
                    exit_code = 1

            if not args.retry_failed_only:
                try:
                    if args.start_date and args.end_date:
                        ready = run_date_range_pipeline(
                            start_date_str=args.start_date,
                            end_date_str=args.end_date,
                            step=args.step,
                            categories=categories,
                            limit=args.limit,
                            settings=settings,
                        )
                    else:
                        ready = run_pipeline(
                            date_str=date_str,
                            step=args.step,
                            categories=categories,
                            limit=args.limit,
                            settings=settings,
                        )
                    if ready:
                        clear_failed_run(settings, current_spec)
                except Exception as exc:
                    if args.step != "init-db":
                        record_failed_run(settings, current_spec, exc)
                    raise
    except PipelineAlreadyRunning as exc:
        print(f"[state] {exc}", file=sys.stderr)
        sys.exit(2)

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
