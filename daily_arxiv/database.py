"""Database schema and persistence helpers for summarized arXiv papers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from config.settings import Settings, load_settings
from daily_arxiv.models import SummarizedPaper
from daily_arxiv.summary_ai import normalize_keyword


class Base(DeclarativeBase):
    pass


paper_keywords = Table(
    "paper_keywords",
    Base.metadata,
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("keyword_id", ForeignKey("keywords.id", ondelete="CASCADE"), primary_key=True),
)

paper_fields = Table(
    "paper_fields",
    Base.metadata,
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("field_id", ForeignKey("fields.id", ondelete="CASCADE"), primary_key=True),
)


class PaperRow(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    arxiv_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    title_kor: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    keywords_json: Mapped[str] = mapped_column("keywords", Text)
    field: Mapped[str] = mapped_column(String(128), index=True)
    fields_json: Mapped[str | None] = mapped_column("fields", Text, nullable=True)
    link: Mapped[str] = mapped_column(Text)
    abstract: Mapped[str] = mapped_column(Text)
    authors_json: Mapped[str] = mapped_column("authors", Text)
    primary_category: Mapped[str] = mapped_column(String(64), index=True)
    arxiv_categories_json: Mapped[str | None] = mapped_column("arxiv_categories", Text, nullable=True)
    matched_categories_json: Mapped[str | None] = mapped_column("matched_categories", Text, nullable=True)
    listing_date: Mapped[str] = mapped_column(String(10), index=True)
    published_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class KeywordRow(Base):
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    normalized_name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class FieldRow(Base):
    __tablename__ = "fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


def make_engine(settings: Settings | None = None) -> Engine:
    settings = settings or load_settings()
    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    return create_engine(settings.database_url, future=True, connect_args=connect_args)


def _ensure_paper_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    if not inspector.has_table("papers"):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("papers")}
    columns_to_add = {
        "fields": "TEXT",
        "arxiv_categories": "TEXT",
        "matched_categories": "TEXT",
    }
    with engine.begin() as connection:
        for name, ddl_type in columns_to_add.items():
            if name not in existing_columns:
                connection.execute(text(f"ALTER TABLE papers ADD COLUMN {name} {ddl_type}"))


def init_db(engine: Engine | None = None, settings: Settings | None = None) -> Engine:
    engine = engine or make_engine(settings)
    Base.metadata.create_all(engine)
    _ensure_paper_columns(engine)
    print("[db] schema is ready")
    return engine


def _get_or_create_keyword(session: Session, keyword: str) -> KeywordRow:
    normalized = normalize_keyword(keyword)
    row = session.scalar(select(KeywordRow).where(KeywordRow.normalized_name == normalized))
    if row:
        return row

    row = KeywordRow(name=keyword, normalized_name=normalized, usage_count=0)
    session.add(row)
    session.flush()
    return row


def _get_or_create_field(session: Session, field: str) -> FieldRow:
    row = session.scalar(select(FieldRow).where(FieldRow.name == field))
    if row:
        return row

    row = FieldRow(name=field, usage_count=0)
    session.add(row)
    session.flush()
    return row


def _upsert_paper(session: Session, paper: SummarizedPaper) -> PaperRow:
    row = session.scalar(select(PaperRow).where(PaperRow.arxiv_id == paper.arxiv_id))
    if row is None:
        row = PaperRow(arxiv_id=paper.arxiv_id)
        session.add(row)

    row.title = paper.title
    row.title_kor = paper.title_kor
    row.summary = paper.summary
    row.keywords_json = json.dumps(paper.keywords, ensure_ascii=False)
    row.field = paper.field
    row.fields_json = json.dumps(paper.fields, ensure_ascii=False)
    row.link = paper.link
    row.abstract = paper.abstract
    row.authors_json = json.dumps(paper.authors, ensure_ascii=False)
    row.primary_category = paper.primary_category
    row.arxiv_categories_json = json.dumps(paper.arxiv_categories, ensure_ascii=False)
    row.matched_categories_json = json.dumps(paper.matched_categories, ensure_ascii=False)
    row.listing_date = paper.listing_date
    row.published_at = paper.published_at
    row.updated_at = paper.updated_at
    row.modified_at = datetime.now(timezone.utc)
    session.flush()
    return row


def _refresh_keyword_usage(session: Session) -> None:
    keyword_rows = session.scalars(select(KeywordRow)).all()
    for keyword in keyword_rows:
        count = session.scalar(
            select(func.count())
            .select_from(paper_keywords)
            .where(paper_keywords.c.keyword_id == keyword.id)
        )
        keyword.usage_count = int(count or 0)


def _refresh_field_usage(session: Session) -> None:
    field_rows = session.scalars(select(FieldRow)).all()
    for field in field_rows:
        count = session.scalar(
            select(func.count())
            .select_from(paper_fields)
            .where(paper_fields.c.field_id == field.id)
        )
        field.usage_count = int(count or 0)


def _retention_cutoff_date(settings: Settings) -> str:
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    return (today - timedelta(days=settings.database_retention_days)).isoformat()


def _cleanup_old_papers_in_session(session: Session, settings: Settings) -> int:
    cutoff = _retention_cutoff_date(settings)
    old_paper_ids = session.scalars(
        select(PaperRow.id).where(PaperRow.listing_date < cutoff)
    ).all()
    if not old_paper_ids:
        return 0

    session.execute(delete(paper_keywords).where(paper_keywords.c.paper_id.in_(old_paper_ids)))
    session.execute(delete(paper_fields).where(paper_fields.c.paper_id.in_(old_paper_ids)))
    session.execute(delete(PaperRow).where(PaperRow.id.in_(old_paper_ids)))
    return len(old_paper_ids)


def cleanup_old_papers(
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
) -> int:
    settings = settings or load_settings()
    engine = init_db(engine=engine, settings=settings)

    with Session(engine) as session:
        deleted = _cleanup_old_papers_in_session(session, settings)
        _refresh_keyword_usage(session)
        _refresh_field_usage(session)
        session.commit()

    if deleted:
        print(f"[db] deleted {deleted} papers older than {_retention_cutoff_date(settings)}")
    return deleted


def save_summarized_papers_to_db(
    papers: list[SummarizedPaper],
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
) -> int:
    settings = settings or load_settings()
    engine = init_db(engine=engine, settings=settings)

    with Session(engine) as session:
        for paper in papers:
            row = _upsert_paper(session, paper)
            session.execute(delete(paper_keywords).where(paper_keywords.c.paper_id == row.id))
            session.execute(delete(paper_fields).where(paper_fields.c.paper_id == row.id))

            linked_keyword_ids: set[int] = set()
            for keyword in paper.keywords:
                keyword_row = _get_or_create_keyword(session, keyword)
                if keyword_row.id in linked_keyword_ids:
                    continue
                linked_keyword_ids.add(keyword_row.id)
                session.execute(
                    paper_keywords.insert().values(
                        paper_id=row.id,
                        keyword_id=keyword_row.id,
                    )
                )

            linked_field_ids: set[int] = set()
            for field in paper.fields:
                field_row = _get_or_create_field(session, field)
                if field_row.id in linked_field_ids:
                    continue
                linked_field_ids.add(field_row.id)
                session.execute(
                    paper_fields.insert().values(
                        paper_id=row.id,
                        field_id=field_row.id,
                    )
                )

        deleted = _cleanup_old_papers_in_session(session, settings) if settings.database_auto_cleanup else 0
        _refresh_keyword_usage(session)
        _refresh_field_usage(session)
        session.commit()

    print(f"[db] saved {len(papers)} summarized papers")
    if deleted:
        print(f"[db] deleted {deleted} papers older than {_retention_cutoff_date(settings)}")
    return len(papers)
