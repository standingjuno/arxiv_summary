# arxiv_summary

arXiv 논문을 매일 수집하고, AI로 한국어 요약/키워드 정리를 수행한 뒤 데이터베이스에 저장하는 개인 연구 아카이브 파이프라인입니다.

현재 범위는 3단계입니다.

1. arXiv API로 논문 메타데이터와 abstract 수집
2. OpenAI Batch API로 제목 번역, 1줄 요약, 키워드 5개 생성
3. 요약 결과를 데이터베이스에 저장

추가로 GitHub Pages용 정적 웹과, 빈 날짜를 선택했을 때 서버에서 실시간 백필을 수행하는 FastAPI API가 붙어 있습니다.

## Architecture

```text
arxiv_summary/
├── main.py                         # CLI 진입점, 전체 파이프라인 오케스트레이션
├── daily_arxiv/
│   ├── fetch_arxiv.py              # arXiv API 수집 함수
│   ├── summary_ai.py               # OpenAI 요약 및 키워드 재사용 로직
│   ├── database.py                 # DB 스키마 생성 및 저장 함수
│   ├── models.py                   # RawPaper, SummaryResult, SummarizedPaper 모델
│   ├── run_state.py                # 실행 lock 및 실패 재시도 큐
│   ├── static_export.py            # DB -> GitHub Pages JSON export
│   └── api.py                      # 빈 날짜 실시간 백필용 HTTP API
├── config/
│   ├── settings.py                 # TOML 설정 로더
│   ├── settings.example.toml       # 공유용 설정 예시
│   └── settings.toml               # 실제 서버 설정, git ignore 대상
├── docker/
│   ├── Dockerfile                  # Python 앱 이미지
│   ├── docker-compose.yml          # arxiv-summary + Postgres 운영 구성
│   ├── Caddyfile                   # HTTPS API reverse proxy
│   └── run_daily.sh                # 컨테이너 내부 일일 실행 스케줄러
├── web/                            # GitHub Pages 정적 웹
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   ├── config.js                   # 공개 API 주소 주입
│   └── data/site-data.json
├── tools/publish_web.sh            # 웹 데이터 export 후 블로그 하위 경로 배포
├── tools/deploy_blog_page.sh       # Jekyll 없이 web/만 standingjuno.github.io/arxiv_summary/로 복사
├── .github/workflows/pages.yml     # GitHub Pages 배포 workflow
├── docs/HANDOFF.md                 # 다음 작업자를 위한 인수인계 기록
└── requirements.txt
```

## Pipeline Order

`main.py`의 `run_pipeline()`이 아래 순서로 실행합니다.

1. `config.settings.load_settings()`
   `config/settings.toml`을 읽어 경로, arXiv 카테고리, OpenAI 설정, DB URL을 준비합니다.

2. `daily_arxiv.fetch_arxiv.fetch_arxiv_papers()`
   `arxiv.categories`에 지정된 카테고리를 arXiv API로 조회합니다. 기본값은 `cs.RO`, `cs.LG`, `cs.CV`입니다. cross-list 논문은 버리지 않고, 설정된 카테고리와 매핑되는 모든 `fields`를 함께 저장합니다.

3. `daily_arxiv.summary_ai.summarize_papers()`
   기본값은 Batch API 모드입니다. 제목과 abstract를 JSONL batch로 묶어 OpenAI에 제출하고, 완료된 batch를 다음 실행에서 회수해 다음 값을 생성합니다.
   `title`, `title_kor`, `summary`, `keywords`, `field`, `link`

4. `daily_arxiv.database.save_summarized_papers_to_db()`
   batch 결과가 준비된 날짜의 요약을 `papers`, `keywords`, `paper_keywords`, `fields`, `paper_fields` 테이블에 저장합니다.

5. `daily_arxiv.static_export.export_static_site_data()`
   DB의 최근 1년치 논문을 `web/data/site-data.json`으로 export합니다. GitHub Pages 웹은 이 JSON을 읽어 화면을 렌더링합니다.

## Config

실제 설정은 `config/settings.toml`에서 관리합니다. 새 환경에서는 예시 파일을 복사해서 시작합니다.

```bash
cp config/settings.example.toml config/settings.toml
```

주요 설정:

```toml
[ai]
provider = "openai"
mode = "batch"
openai_api_key = "sk-..."
openai_model = "gpt-5.4-nano"
openai_batch_completion_window = "24h"
openai_batch_wait_timeout_seconds = 0.0

[arxiv]
categories = ["cs.RO", "cs.LG", "cs.CV"]
delay_seconds = 5.0
min_request_interval = 5.0
retry_delays = [30.0, 60.0, 180.0, 600.0]

[keywords]
seed = ["SLAM", "LiDAR", "LIO", "LO", "Retargeting", "LLM", "VLM", "VLN", "VLA", "RL", "IL", "Diffusion", "Manipulation", "Navigation", "Sim2Real"]

[database]
url = "postgresql+psycopg://arxiv_summary:change-this-password@postgres:5432/arxiv_summary"
retention_days = 365
auto_cleanup = true

[daily]
run_hour = 11
run_minute = 0
retry_failed = true
run_on_start = false

[web]
output_dir = "web"
base_path = "/arxiv_summary/"
api_base_url = "https://arxiv-summary.221.155.32.232.sslip.io"
export_days = 365
auto_export = true

[api]
host = "0.0.0.0"
port = 8000
cors_origins = ["https://standingjuno.github.io"]
on_demand_enabled = true
on_demand_summary_mode = "sync"
on_demand_max_papers = 600
```

머신러닝/LLM 쪽을 더 넓히고 싶으면 `cs.CL`, `cs.AI`, `stat.ML` 등을 추가할 수 있습니다.

```toml
[arxiv]
categories = ["cs.RO", "cs.LG", "cs.CV", "stat.ML", "cs.CL", "cs.AI"]
```

`CONFIG_PATH` 환경변수나 `--config` 옵션으로 다른 설정 파일을 지정할 수 있습니다.

## Web

정적 웹은 `web/` 폴더에 있습니다.

- `web/index.html`: 화면 뼈대
- `web/styles.css`: 흰색 배경의 정돈된 UI 스타일
- `web/app.js`: 필드 탭, 날짜 달력, 키워드 검색, 논문 렌더링, 실시간 백필 job 폴링
- `web/config.js`: GitHub Pages에서 호출할 공개 API base URL
- `web/data/site-data.json`: GitHub Pages가 읽는 데이터 파일

화면 기능:

- `All`, `cs.RO`, `cs.LG`, `cs.CV` 네비게이션 탭
- 주말이 잠긴 달력
- 날짜 선택 및 전체 날짜 보기
- 현재 존재하는 키워드 검색/필터
- 날짜별 논문 그룹 표시
- 데이터가 없는 평일 날짜를 선택하면 API가 arXiv 수집, OpenAI 요약, DB 저장, 웹 JSON export를 수행
- 백필 중에는 원형 spinner와 진행률이 표시됨
- 달력은 현재 월을 포함한 최근 12개월만 보여주며, 오늘 이후 날짜와 주말은 선택할 수 없음

GitHub Pages는 정적 호스팅이라 OpenAI API나 DB를 직접 실행할 수 없습니다. 그래서 `standingjuno.github.io/arxiv_summary/`는 정적 UI를 담당하고, 서버의 `arxiv-summary-api` 컨테이너가 실시간 백필을 담당합니다.

서버 Caddy도 같은 정적 웹을 `https://arxiv-summary.221.155.32.232.sslip.io/arxiv_summary/`에서 제공합니다. 이 주소는 `web/` 파일을 서버 디스크에서 바로 읽고 `/api/*`, `/health`만 FastAPI로 reverse proxy하므로 GitHub Pages 배포 지연과 무관하게 즉시 확인할 수 있습니다.

이 페이지 자체는 Jekyll을 사용하지 않습니다. `web/` 안의 파일은 front matter, Liquid include, Jekyll layout 없이 순수 HTML/CSS/JS/JSON으로 동작합니다.

공개 API 주소를 연결하는 방법은 둘 중 하나입니다.

```js
// web/config.js
window.ARXIV_SUMMARY_CONFIG = {
  apiBaseUrl: 'https://arxiv-summary.221.155.32.232.sslip.io',
};
```

또는 `config/settings.toml`에 아래처럼 넣고 `export-web`를 실행하면 `site-data.json`에 포함됩니다.

```toml
[web]
api_base_url = "https://arxiv-summary.221.155.32.232.sslip.io"
```

DB에서 웹 데이터를 수동으로 갱신:

```bash
python main.py --step export-web
```

export 후 GitHub Pages에 반영하려면 커밋/푸시가 필요합니다. 서버에서 바로 갱신할 때는:

```bash
./tools/publish_web.sh
```

이 스크립트는 Docker Compose 안에서 export를 실행해 Postgres에 접근한 뒤, `web/` 전체를 `standingjuno/standingjuno.github.io` 저장소의 `arxiv_summary/` 폴더로 복사하고 push합니다. 로컬/서버에서 Jekyll을 설치하거나 실행하지 않습니다.

정적 페이지만 다시 배포하려면 export 없이 아래 명령을 사용합니다.

```bash
./tools/deploy_blog_page.sh
```

환경변수로 대상 저장소와 하위 경로를 바꿀 수 있습니다.

```bash
BLOG_REPO_URL=git@github.com:standingjuno/standingjuno.github.io.git \
BLOG_SUBDIR=arxiv_summary \
./tools/deploy_blog_page.sh
```

## GitHub Pages

현재 실제 공개 URL은 `standingjuno/standingjuno.github.io` 저장소의 하위 폴더 배포 방식으로 만듭니다.

```text
standingjuno/standingjuno.github.io
└── arxiv_summary/
    ├── index.html
    ├── styles.css
    ├── app.js
    ├── config.js
    └── data/site-data.json
```

목표 GitHub Pages 주소는 `https://standingjuno.github.io/arxiv_summary/`입니다.

완전히 repo 단위로 Jekyll과 분리하고 싶으면 `standingjuno/arxiv_summary` 저장소를 새로 만들거나 기존 저장소 이름을 `arxiv_summary`로 변경하면 됩니다. 그 경우 `.github/workflows/pages.yml`이 `web/` 폴더를 GitHub Pages artifact로 배포하므로 Jekyll이 전혀 필요 없습니다.

이번 웹은 상대 경로로 asset/data를 읽으므로 `arxiv_summary` 하위 경로에서도 그대로 동작합니다.

## Batch Summary Flow

OpenAI 요약은 기본적으로 Batch API를 사용합니다. Batch API는 요청 묶음을 비동기로 처리하며, 일반 동기 API보다 비용이 낮고 별도 rate limit pool을 사용하지만 결과가 즉시 나오지는 않습니다.

실행 흐름:

1. 오늘의 arXiv raw JSON을 저장합니다.
2. 아직 요약되지 않은 논문들을 `output/batches/summary_batch_YYYY-MM-DD.input.jsonl`로 만듭니다.
3. 해당 JSONL을 OpenAI Files API에 `purpose = "batch"`로 업로드합니다.
4. `/v1/chat/completions` batch를 생성하고 상태를 `summary_batch_YYYY-MM-DD.state.json`에 저장합니다.
5. 다음 실행에서 완료된 batch를 확인하고 `summary_batch_YYYY-MM-DD.output.jsonl`을 내려받습니다.
6. `custom_id`로 논문을 다시 매핑해 `output/summaries/arxiv_summaries_YYYY-MM-DD.json`과 DB에 저장합니다.

제출 직후 batch가 `validating` 또는 `in_progress` 상태이면 정상 대기 상태로 보고, `store`는 다음 실행에서 처리합니다. 즉 daily job은 보통 “완료된 이전 batch 반영 + 오늘 batch 제출” 방식으로 움직입니다.

## Watched Fields

현재 기본으로 보는 arXiv 카테고리는 `cs.RO`, `cs.LG`, `cs.CV`입니다.

- `cs.RO`: Robotics. 프로젝트에서는 `robotics` 필드로 매핑합니다.
- `cs.LG`: Machine Learning. 프로젝트에서는 `machine_learning` 필드로 매핑합니다.
- `cs.CV`: Computer Vision and Pattern Recognition. 프로젝트에서는 `computer_vision` 필드로 매핑합니다.

추가 후보는 이미 `config/settings.toml`의 `[fields]`에 준비되어 있습니다.

- `stat.ML`: 통계/이론 기반 Machine Learning. `machine_learning` 필드로 매핑합니다.
- `cs.AI`: Artificial Intelligence. 현재는 머신러닝 확장 후보로 보고 `machine_learning` 필드로 매핑합니다.
- `cs.CL`: Computation and Language, 즉 NLP/언어 모델 계열. 현재는 `machine_learning` 필드로 매핑합니다.

논문 하나가 `cs.RO`, `cs.LG`, `cs.CV`에 동시에 걸리면 다음처럼 저장됩니다.

```json
{
  "primary_category": "cs.RO",
  "arxiv_categories": ["cs.RO", "cs.LG", "cs.CV"],
  "matched_categories": ["cs.RO", "cs.LG", "cs.CV"],
  "field": "robotics",
  "fields": ["robotics", "machine_learning", "computer_vision"]
}
```

나중에 웹에서는 `paper_fields` 테이블 또는 `papers.fields` JSON을 기준으로 네비게이션 탭을 만들면 됩니다.

## Keywords

`config/settings.toml`의 `[keywords]`는 초기 키워드 사전입니다. 처음 실행할 때 `output/keywords.json`에 들어가고, 이후 AI가 새 키워드를 만들면 같은 파일에 계속 추가됩니다.

축약어를 우선 쓰기 위해 `[keywords.aliases]`를 둡니다. 예를 들어 AI가 `Reinforcement Learning`을 반환해도 저장 단계에서 `RL`로 바뀌고, `Vision-Language Model`은 `VLM`, `Imitation Learning`은 `IL`, `Diffusion Policy`는 `Diffusion`으로 정리됩니다.

## Output Files

기본 출력 경로는 `app.output_dir`입니다.

```text
output/
├── raw/arxiv_raw_YYYY-MM-DD.json
├── summaries/arxiv_summaries_YYYY-MM-DD.json
├── batches/summary_batch_YYYY-MM-DD.input.jsonl
├── batches/summary_batch_YYYY-MM-DD.state.json
├── batches/summary_batch_YYYY-MM-DD.output.jsonl
├── batches/summary_batch_YYYY-MM-DD.errors.jsonl
├── keywords.json
├── failed_runs.json
├── pipeline.lock
└── arxiv_summary.db               # SQLite 사용 시 생성
```

Docker Compose 운영에서는 `database.url`이 Postgres를 바라보므로 SQLite 파일은 만들지 않습니다.

## Database

`papers`

- `title`: 원문 제목
- `title_kor`: 한국어 제목
- `summary`: abstract 기반 한국어 1문장 요약
- `keywords`: 키워드 5개 JSON 문자열
- `field`: 대표 필드. 기본값은 `robotics`, `machine_learning`, `computer_vision` 중 하나입니다.
- `fields`: 논문이 속한 프로젝트 필드 목록 JSON 문자열
- `link`: arXiv 링크
- 추가 메타데이터: `arxiv_id`, `abstract`, `authors`, `primary_category`, `arxiv_categories`, `matched_categories`, `listing_date`, `published_at`, `updated_at`

`keywords`

- 키워드 사전입니다.
- 이미 사용된 키워드는 정규화된 이름으로 재사용합니다.

`paper_keywords`

- 논문과 키워드의 N:M 연결 테이블입니다.

`fields`

- 웹 네비게이션용 프로젝트 필드 사전입니다.

`paper_fields`

- 논문과 프로젝트 필드의 N:M 연결 테이블입니다.

## Retention

DB는 기본적으로 최근 365일만 보관합니다.

```toml
[database]
retention_days = 365
auto_cleanup = true
```

`auto_cleanup = true`이면 `save_summarized_papers_to_db()`가 실행될 때마다 `listing_date`가 retention window보다 오래된 논문을 `papers`, `paper_keywords`, `paper_fields`에서 삭제하고 키워드/필드 usage count를 다시 계산합니다.

수동 정리는 아래 명령으로 실행합니다.

```bash
python main.py --step cleanup-db
```

## Docker Run

서버에서 계속 켜두는 기본 운영 방식입니다.

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

`docker/docker-compose.yml`은 다음 서비스를 올립니다.

- `postgres`: 1년치 요약 데이터 저장
- `api`: FastAPI 백필/조회 서버
- `caddy`: `https://arxiv-summary.221.155.32.232.sslip.io` HTTPS reverse proxy와 `/arxiv_summary/` 정적 웹
- `arxiv-summary`: 오전 11시 daily scheduler

상태 확인:

```bash
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs -f arxiv-summary
docker compose -f docker/docker-compose.yml logs -f api
docker compose -f docker/docker-compose.yml logs -f caddy
```

DB 초기화:

```bash
docker compose -f docker/docker-compose.yml run --rm arxiv-summary python main.py --step init-db
```

수동 1회 실행:

```bash
docker compose -f docker/docker-compose.yml run --rm arxiv-summary python main.py --date 2026-07-23 --step all
```

스케줄러는 `config/settings.toml`의 `[daily]` 값을 읽습니다. 기본 권장값은 한국 시간 오전 11시입니다. 매 실행 전에 완료된 batch를 회수해 DB에 저장하고, `failed_runs.json`에 남은 실패 작업을 먼저 재시도합니다.

## Local Run

Docker 없이 SQLite로 빠르게 확인하려면 `config/settings.toml`의 `[database].url`을 빈 문자열로 두면 됩니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py --step init-db
python main.py --date 2026-07-23 --step fetch
python main.py --date 2026-07-23 --step summary --limit 3
python main.py --date 2026-07-24 --step store
```

`summary` 단계는 `config/settings.toml`의 `ai.openai_api_key`가 필요합니다. Batch 결과가 아직 준비되지 않았으면 `store`는 다음 실행에서 처리합니다.

## CLI

```bash
python main.py --step fetch
python main.py --step summary --limit 5
python main.py --step store
python main.py --step all
python main.py --step init-db
python main.py --step export-web
python main.py --step cleanup-db
python main.py --config config/settings.toml --step fetch
```

최근 여러 날짜를 백필할 때는 날짜별 API 호출을 반복하지 말고 range 옵션을 사용합니다.

```bash
python main.py --start-date 2026-07-10 --end-date 2026-07-23 --step fetch --categories cs.RO,cs.LG
python main.py --start-date 2026-07-10 --end-date 2026-07-23 --step all --categories cs.RO,cs.LG
```

실패 큐만 수동 재시도:

```bash
python main.py --retry-failed-only
```

## arXiv 429 대응

arXiv API Terms of Use는 legacy API에 대해 관리 중인 모든 머신 전체 기준으로 3초에 1요청 이하, single connection 사용을 요구합니다. 이 프로젝트는 기본적으로 5초 간격을 두고, 429 또는 `Rate exceeded`가 발생하면 `30초 -> 60초 -> 180초 -> 600초` 순서로 backoff합니다.

429가 발생한 실행은 `failed_runs.json`에 남겨 다음 스케줄에서 다시 수행합니다. Batch 결과 JSON은 논문 단위로 저장되므로, 결과 일부만 성공한 경우 재실행하면 이미 처리된 논문을 건너뛰고 남은 논문만 새 batch로 제출합니다.

## Next Roadmap

- API HTTPS 노출 구성
- `standingjuno.github.io/arxiv_summary/` 경로에 맞춘 배포 저장소/브랜치 전략 확정
- 실시간 백필 job 히스토리 영속화
- 로컬 AI provider 추가
- 테스트와 DB migration 도입
