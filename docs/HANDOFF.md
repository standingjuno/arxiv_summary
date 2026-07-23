# arxiv_summary Handoff

작성일: 2026-07-23

## 현재 구조

루트에는 `main.py`만 실행 진입점으로 남기고, 기능 코드는 `daily_arxiv/`, 설정은 `config/`, Docker 운영 파일은 `docker/`로 분리했다.

```text
main.py
  -> config.settings.load_settings()
  -> daily_arxiv.fetch_arxiv.fetch_arxiv_papers()
  -> daily_arxiv.summary_ai.summarize_papers()
  -> daily_arxiv.database.save_summarized_papers_to_db()
  -> daily_arxiv.static_export.export_static_site_data()
```

## 생성/수정 파일

- `daily_arxiv/fetch_arxiv.py`
  - arXiv API 조회 전담
  - 기본 카테고리: `cs.RO`, `cs.LG`, `cs.CV`
  - cross-list 논문도 수집
  - 한 논문이 여러 watch category에 속하면 `matched_categories`와 `fields`를 병합
  - raw JSON 저장: `output/raw/arxiv_raw_YYYY-MM-DD.json`
  - range backfill은 카테고리별 넓은 기간을 한 번에 가져온 뒤 날짜별로 나눔

- `daily_arxiv/summary_ai.py`
  - 기본값은 OpenAI Batch API 호출
  - `ai.mode = "sync"`로 바꾸면 기존 Chat Completions 동기 호출도 가능
  - 기본 모델은 `gpt-5.4-nano`
  - 원문 제목, 한국어 제목, 한국어 1문장 요약, 키워드 최대 5개 생성
  - batch 입력/상태/출력 파일 저장:
    - `output/batches/summary_batch_YYYY-MM-DD.input.jsonl`
    - `output/batches/summary_batch_YYYY-MM-DD.state.json`
    - `output/batches/summary_batch_YYYY-MM-DD.output.jsonl`
    - `output/batches/summary_batch_YYYY-MM-DD.errors.jsonl`
  - 키워드 사전 JSON 저장: `output/keywords.json`
  - 기존 키워드는 정규화 이름 기준으로 재사용
  - 축약어 우선 정책 적용: `Reinforcement Learning -> RL`, `Imitation Learning -> IL`, `Vision-Language Model -> VLM`, `Vision-Language Navigation -> VLN`, `Diffusion Policy -> Diffusion`
  - 로컬 AI 전환을 위해 `ai.provider` 경계를 둠

- `daily_arxiv/database.py`
  - SQLAlchemy 기반 DB 저장
  - SQLite와 Postgres 모두 지원
  - `papers`, `keywords`, `paper_keywords`, `fields`, `paper_fields` 테이블 생성
  - 기존 DB에 `papers.fields`, `papers.arxiv_categories`, `papers.matched_categories` 컬럼이 없으면 간단한 ALTER TABLE로 추가
  - `database.auto_cleanup = true`이면 저장 시 오래된 DB 데이터를 자동 삭제
  - 수동 정리 step: `python main.py --step cleanup-db`

- `daily_arxiv/static_export.py`
  - DB의 최근 1년치 논문을 GitHub Pages용 JSON으로 export
  - 기본 출력: `web/data/site-data.json`
  - category nav, dates, keywords, papers payload 포함
  - `api_base_url`, `on_demand_enabled`, `retention_days`도 payload에 포함
  - 날짜 데이터는 주말 잠금 UI를 위해 `is_weekend` 값을 포함

- `daily_arxiv/api.py`
  - FastAPI 기반 실시간 백필 API
  - `GET /health`
  - `GET /api/data`: 현재 `web/data/site-data.json` 반환
  - `POST /api/dates/{YYYY-MM-DD}/run`: 특정 날짜 데이터가 DB에 없으면 수집/요약/저장/export job 생성
  - `GET /api/jobs/{job_id}`: UI 진행률 폴링
  - 1년 retention window 밖 날짜, 미래 날짜, 주말 날짜는 400으로 거절
  - daily run과 충돌하지 않도록 `pipeline.lock`을 재사용
  - 기본 on-demand 요약은 `api.on_demand_summary_mode = "sync"`라서 사용자가 기다리는 동안 완료를 목표로 함

- `daily_arxiv/models.py`
- `RawPaper`
  - `arxiv_categories`, `matched_categories`, `fields` 리스트를 포함
- `SummaryResult`
- `SummarizedPaper`

- `daily_arxiv/run_state.py`
  - 파일 lock 기반 중복 실행 방지
  - 실패한 날짜/단계를 `output/failed_runs.json`에 기록
  - 다음 스케줄 또는 `--retry-failed-only` 실행 시 실패 큐 재처리

- `config/settings.py`
  - `.env` 기반 로딩 제거
  - `config/settings.toml` 또는 `CONFIG_PATH`가 가리키는 TOML 파일을 읽음
  - `python -m config.settings daily.run_hour`처럼 스케줄러에서 값을 조회할 수 있는 작은 CLI 포함
  - `[keywords]`의 seed와 aliases를 `Settings`에 포함
  - `[ai]`의 Batch API 옵션을 `Settings`에 포함
  - `[web]`의 `output_dir`, `base_path`, `export_days`, `auto_export`를 `Settings`에 포함
  - `[api]`의 CORS, on-demand, max paper 설정을 `Settings`에 포함
  - `[database]`의 `retention_days`, `auto_cleanup`을 `Settings`에 포함

- `config/settings.example.toml`
  - 공유용 설정 템플릿

- `config/settings.toml`
  - 실제 서버 설정 파일
  - `.gitignore`와 `.dockerignore` 대상
  - `ai.openai_api_key`는 여기 채워야 함

- `docker/Dockerfile`, `docker/docker-compose.yml`, `docker/Caddyfile`, `docker/run_daily.sh`
  - 서버 상시 실행 구성
  - Postgres 컨테이너 포함
  - `api` 서비스 추가: `daily_arxiv.api:app`을 uvicorn으로 실행, 기본 host port `8000`
  - `caddy` 서비스 추가: `https://arxiv-summary.221.155.32.232.sslip.io`에서 `/api/*`, `/health`는 `api:8000`으로 reverse proxy하고 `/arxiv_summary/`는 `web/` 정적 파일을 serve
  - `restart: unless-stopped`
  - 기본 daily run 시간은 KST 11:00
  - 매 실행 전 실패 큐 재시도
  - 앱 컨테이너는 기본 탐색 순서대로 `/app/config/settings.toml`을 읽고, 없으면 예시 설정을 fallback으로 읽음
  - 앱 컨테이너는 기본 `1000:1000` 사용자로 실행해 bind mount output이 root 소유로 남지 않게 함
  - `web/`을 `/app/web`으로 mount해서 컨테이너 내부 export가 GitHub Pages 데이터 파일을 갱신함

- `main.py`
  - `fetch`, `summary`, `store`, `export-web`, `cleanup-db`, `init-db`, `all` 단계 지원
  - `--start-date`, `--end-date` 기반 range backfill 지원
  - `--retry-failed`, `--retry-failed-only` 지원
  - `--config`로 별도 TOML 설정 파일 지정 가능
  - Batch API가 pending 상태이면 실패로 기록하지 않고 정상 대기 처리
  - `summary/store/all` 실행 전 완료된 기존 batch를 먼저 회수하고, 필요한 경우 DB에 저장
  - DB 저장 후 `web.auto_export = true`이면 정적 웹 JSON 자동 갱신

- `web/index.html`, `web/styles.css`, `web/app.js`
  - GitHub Pages용 정적 웹
  - `All`, `cs.RO`, `cs.LG`, `cs.CV` 네비게이션 탭
  - 주말이 잠긴 custom calendar
  - 키워드 검색/필터
  - 날짜별 논문 카드 표시
  - 빈 평일 날짜 선택 시 서버 API에 백필 job 요청
  - 백필 진행 중 원형 spinner와 progress percent 표시

- `web/config.js`
  - 공개 API URL 설정 파일
  - 예: `apiBaseUrl: "https://arxiv-summary.221.155.32.232.sslip.io"`

- `.github/workflows/pages.yml`
  - `web/` 폴더를 GitHub Pages artifact로 배포
  - 별도 `standingjuno/arxiv_summary` 저장소를 만들 경우 GitHub repo Settings > Pages에서 source를 GitHub Actions로 설정하면 Jekyll 없이 배포 가능

- `tools/publish_web.sh`
  - 서버에서 `docker compose -f docker/docker-compose.yml run --rm arxiv-summary python main.py --step export-web` 실행
  - 이후 `tools/deploy_blog_page.sh`를 호출해 `web/` 전체를 블로그 저장소의 `arxiv_summary/` 폴더로 push

- `tools/deploy_blog_page.sh`
  - Jekyll을 설치/실행하지 않고 `web/` 정적 파일만 `standingjuno/standingjuno.github.io`의 `arxiv_summary/` 폴더로 복사
  - 기본 URL: `https://standingjuno.github.io/arxiv_summary/`
  - 환경변수: `BLOG_REPO_URL`, `BLOG_BRANCH`, `BLOG_SUBDIR`, `COMMIT_MESSAGE`

## DB 스키마

`papers`

- `arxiv_id`: unique
- `title`
- `title_kor`
- `summary`
- `keywords`: JSON 문자열
- `field`
- `fields`: JSON 문자열
- `link`
- `abstract`
- `authors`: JSON 문자열
- `primary_category`
- `arxiv_categories`: JSON 문자열
- `matched_categories`: JSON 문자열
- `listing_date`
- `published_at`
- `updated_at`

`keywords`

- `name`
- `normalized_name`: unique
- `usage_count`

`paper_keywords`

- `paper_id`
- `keyword_id`

`fields`

- `name`
- `usage_count`

`paper_fields`

- `paper_id`
- `field_id`

## 주요 설계 결정

- `field`와 `fields`는 AI가 추측하지 않고 arXiv category에서 결정한다.
- `cs.RO`는 `robotics`, `cs.LG/stat.ML/cs.AI/cs.CL`은 `machine_learning`, `cs.CV`는 `computer_vision`으로 매핑했다.
- cross-list 때문에 하나의 논문은 여러 `fields`를 가질 수 있다. 예: `cs.RO + cs.LG + cs.CV -> robotics + machine_learning + computer_vision`.
- AI에는 title과 abstract만 전달한다. PDF 전문 파싱은 아직 하지 않는다.
- 웹 달력은 현재 월 포함 최근 12개월만 노출한다. 예: 2026년 7월 기준 `2025-08`부터 `2026-07`까지이며, 오늘 이후 날짜와 주말은 UI와 API에서 모두 막는다.
- OpenAI 요약은 Batch API를 기본으로 사용한다. 한 daily run은 보통 이전 완료 batch를 DB에 반영하고 오늘 batch를 제출한다.
- Batch API 제출 직후 `validating/in_progress/finalizing` 상태는 오류가 아니라 정상 대기 상태다.
- GitHub Pages는 DB에 직접 접근하지 않는다. 반드시 서버에서 DB를 `web/data/site-data.json`으로 export한 뒤 push해야 웹 데이터가 갱신된다.
- 단, `web/config.js` 또는 `site-data.json`에 `api_base_url`이 들어 있으면 Pages UI가 서버 API를 호출해 빈 날짜를 실시간으로 백필할 수 있다.
- GitHub Pages가 HTTPS이므로 API도 실사용에서는 HTTPS로 노출해야 브라우저 mixed content 차단을 피할 수 있다.
- 현재 API 도메인: `https://arxiv-summary.221.155.32.232.sslip.io`
- 서버 미러 웹: `https://arxiv-summary.221.155.32.232.sslip.io/arxiv_summary/`
- 목표 Pages 주소는 `https://standingjuno.github.io/arxiv_summary/`이다. 일반적인 project Pages 구성을 쓰려면 GitHub 저장소 이름을 `arxiv_summary`로 맞춘다.
- 현재 `standingjuno/arxiv_summary` 저장소는 없어서, 실제 공개 배포는 `standingjuno/standingjuno.github.io` 저장소의 `arxiv_summary/` 하위 폴더에 정적 파일을 복사하는 방식이다. 해당 페이지 파일 자체는 Jekyll front matter/layout/include를 쓰지 않는다.
- 키워드 재사용은 현재 두 층이다.
  - 프롬프트에서 기존 키워드 목록을 제공하고 재사용을 지시
  - 코드에서 alias를 먼저 canonical keyword로 바꾼 뒤 normalized keyword가 같으면 기존 표기를 재사용
- 의미적으로 유사한 키워드 병합은 아직 embedding 기반이 아니다. 나중에 추가하면 좋다.
- DB migration 도구는 아직 없다. 현재는 SQLAlchemy `create_all()`만 사용한다.
- arXiv 429 대응을 위해 기본 요청 간격을 5초로 두고, 429 발생 시 30/60/180/600초 backoff한다.
- 스케줄러와 수동 실행이 겹치지 않도록 `pipeline.lock`을 사용한다.
- 실패한 실행은 `failed_runs.json`에 기록되고 다음 스케줄에서 자동 재시도된다. Batch 결과 일부만 성공하면 성공분은 저장되고 남은 논문은 다음 재실행에서 새 batch로 제출된다.
- DB retention은 `database.retention_days = 365`, `database.auto_cleanup = true`가 기본이다. DB 저장 시 오래된 `papers` row와 연결 테이블 row가 삭제된다.

## 운영 방법

Docker 기준 명령:

```bash
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs -f arxiv-summary
docker compose -f docker/docker-compose.yml logs -f api
docker compose -f docker/docker-compose.yml logs -f caddy
```

DB 초기화:

```bash
docker compose -f docker/docker-compose.yml run --rm arxiv-summary python main.py --step init-db
```

수동 실행:

```bash
docker compose -f docker/docker-compose.yml run --rm arxiv-summary python main.py --date 2026-07-23 --step all
```

## 다음 작업 추천

1. `config/settings.toml`의 `ai.openai_api_key` 입력
2. `docker/docker-compose.yml`과 `config/settings.toml`의 Postgres 계정/비밀번호를 운영 값으로 변경
3. 작은 날짜 범위와 `--limit 3`으로 OpenAI batch 제출 확인
4. batch 완료 후 다음 실행에서 summary JSON과 DB 저장 확인
5. 웹/API 운영 연결
   - GitHub Pages source를 GitHub Actions로 설정
   - 현 방식은 `standingjuno.github.io` 저장소의 `arxiv_summary/` 하위 폴더에 `web/`을 복사
   - 완전한 Jekyll-free repo 배포를 원하면 GitHub 저장소 이름과 Pages URL을 `standingjuno/arxiv_summary`, `standingjuno.github.io/arxiv_summary/` 기준으로 맞추기
   - API를 HTTPS로 노출하고 `web/config.js` 또는 `web.api_base_url`에 URL 입력
   - 실제 DB 데이터로 `python main.py --step export-web` 확인
   - `./tools/publish_web.sh`를 서버 cron/운영 플로우에 연결
6. 키워드 고도화
   - keyword alias 테이블
   - embedding 기반 유사 키워드 추천
   - 수동 merge UI
7. 운영 안정화
   - 실패한 논문만 재시도 로그 개선
   - structured logging
   - 테스트 추가
