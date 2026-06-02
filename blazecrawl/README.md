# BlazeCrawl 🔥

A FireCrawl-style web scraper & crawler built with **FastAPI**, **httpx**, and **Playwright**.

BlazeCrawl turns any website into clean, structured data — Markdown, HTML, plain text, or structured JSON — through a small, focused HTTP API.

## Features

- ⚡ **Single-page scraping** with Mozilla Readability-style main-content extraction.
- 🕸️ **Recursive crawling** with BFS, configurable depth, page caps, and allow/deny patterns.
- 🗺️ **Site mapping** via `sitemap.xml`, `robots.txt`, and lightweight anchor discovery.
- 🤖 **JS rendering** via Playwright (Chromium) with auto-scroll for lazy-loaded content.
- 🚦 **Per-domain rate limiting** (token bucket) and **robots.txt** compliance.
- 🔁 **Proxy rotation** with round-robin pool.
- 💾 **In-memory TTL caching** with automatic cleanup.
- 📊 **Structured extraction** of tables, lists, and JSON-LD blocks.
- 🩺 **/health** endpoint with uptime, cache size, and active-job stats.
- 🧱 Fully async, fully type-hinted, structured logging, graceful shutdown.

## Project layout

```
blazecrawl/
├── main.py               # FastAPI app entry point
├── config.py             # Central configuration & constants
├── requirements.txt      # All dependencies
│
├── core/
│   ├── scraper.py        # Single-page scraper logic
│   ├── crawler.py        # Recursive multi-page crawler
│   ├── mapper.py         # Site-wide URL discovery/mapper
│   └── browser.py        # Headless browser manager (Playwright)
│
├── extractors/
│   ├── content.py        # Main content extraction (remove boilerplate)
│   ├── markdown.py       # HTML → clean Markdown converter
│   ├── metadata.py       # Extract title, description, OG tags, etc.
│   └── structured.py     # Extract tables, lists, JSON-LD
│
├── services/
│   ├── rate_limiter.py   # Per-domain token-bucket rate limiting
│   ├── robots.py         # robots.txt parser & compliance checker
│   ├── proxy.py          # Proxy rotation manager
│   └── cache.py          # URL-level caching (in-memory + TTL)
│
├── models/
│   └── schemas.py        # Pydantic request/response models
│
└── utils/
    ├── url.py            # URL normalization, validation, domain extraction
    └── helpers.py        # Retry logic, hashing, user-agent rotation
```

## Setup

Python 3.10+ is required.

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install the Chromium browser used by Playwright
playwright install chromium

# 3. Start the API server
uvicorn main:app --host 0.0.0.0 --port 8000
```

## API usage

### 1. `POST /scrape` — Scrape one page

```bash
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "formats": ["markdown", "html", "text"],
    "only_main_content": true,
    "include_metadata": true,
    "wait_for_js": false,
    "auto_browser_fallback": true,
    "timeout": 30000,
    "remove_selectors": ["nav", ".ads", "footer"]
  }'
```

**Anti-bot handling.** By default (`auto_browser_fallback: true`), BlazeCrawl
first attempts a fast plain-HTTP fetch. If the site blocks bots (HTTP 401, 403,
406, 429, or 503), it transparently retries with the headless Chromium browser
(stealth-patched: `navigator.webdriver` hidden, plugin/language fingerprints
normalised, realistic viewport/locale). You only see an error if both attempts
fail — typically that means commercial bot protection (Cloudflare Turnstile,
DataDome) and you'll need a residential proxy. Set `auto_browser_fallback: false`
to keep the original fast-only behaviour, or `wait_for_js: true` to skip the
HTTP attempt entirely.

### 2. `POST /crawl` — Start a recursive crawl

```bash
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "max_depth": 3,
    "max_pages": 100,
    "formats": ["markdown"],
    "only_main_content": true,
    "include_metadata": true,
    "allow_patterns": ["/blog/*", "/docs/*"],
    "deny_patterns": ["/admin/*", "*.pdf"],
    "wait_for_js": false,
    "respect_robots_txt": true
  }'
```

Response:

```json
{ "success": true, "job_id": "crawl_abc123", "status": "running" }
```

### 3. `GET /crawl/{job_id}` — Poll a crawl

```bash
curl http://localhost:8000/crawl/crawl_abc123
```

### 4. `DELETE /crawl/{job_id}` — Cancel a crawl

```bash
curl -X DELETE http://localhost:8000/crawl/crawl_abc123
```

### 5. `POST /map` — Discover URLs

```bash
curl -X POST http://localhost:8000/map \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "max_pages": 500,
    "include_subdomains": false
  }'
```

### 6. `GET /health` — Liveness probe

```bash
curl http://localhost:8000/health
```

## Configuration

All settings are environment variables prefixed with `BLAZECRAWL_`. A `.env` file in the project root is also read.

| Variable                            | Default                     | Description                                            |
|-------------------------------------|-----------------------------|--------------------------------------------------------|
| `BLAZECRAWL_HOST`                   | `0.0.0.0`                   | Bind host                                              |
| `BLAZECRAWL_PORT`                   | `8000`                      | Bind port                                              |
| `BLAZECRAWL_LOG_LEVEL`              | `INFO`                      | Python log level                                       |
| `BLAZECRAWL_MAX_CONCURRENT_REQUESTS`| `10`                        | Global concurrency hint                                |
| `BLAZECRAWL_CRAWLER_CONCURRENCY`    | `5`                         | Parallel requests per crawl job                        |
| `BLAZECRAWL_REQUEST_TIMEOUT`        | `30`                        | httpx timeout (seconds)                                |
| `BLAZECRAWL_BROWSER_TIMEOUT`        | `30000`                     | Playwright timeout (ms)                                |
| `BLAZECRAWL_NAVIGATION_TIMEOUT`     | `30000`                     | Playwright navigation timeout (ms)                     |
| `BLAZECRAWL_DEFAULT_RPS`            | `2.0`                       | Default per-domain requests/second                     |
| `BLAZECRAWL_DEFAULT_BURST`          | `5`                         | Token-bucket burst capacity                            |
| `BLAZECRAWL_POLITE_DELAY`           | `0.5`                       | Min delay between same-domain hits during a crawl (s)  |
| `BLAZECRAWL_CACHE_TTL`              | `300`                       | Scrape cache TTL (seconds)                             |
| `BLAZECRAWL_CACHE_MAX_SIZE`         | `1000`                      | Max cached scrape entries                              |
| `BLAZECRAWL_MAX_RESPONSE_BYTES`     | `10485760`                  | 10 MB response body cap                                |
| `BLAZECRAWL_MAX_CRAWL_PAGES`        | `1000`                      | Server-side cap on `max_pages`                         |
| `BLAZECRAWL_MAX_CRAWL_DEPTH`        | `10`                        | Server-side cap on `max_depth`                         |
| `BLAZECRAWL_PROXIES`                | *(empty)*                   | Comma-separated list of proxy URLs                     |
| `BLAZECRAWL_BROWSER_HEADLESS`       | `true`                      | Run Chromium headless                                  |
| `BLAZECRAWL_BLOCK_RESOURCES`        | `image,font,media`          | Resource types to block in the browser                 |
| `BLAZECRAWL_AUTO_SCROLL`            | `true`                      | Auto-scroll rendered pages                             |
| `BLAZECRAWL_MAX_RETRIES`            | `3`                         | Async retries on transient errors                      |
| `BLAZECRAWL_RETRY_BASE_DELAY`       | `1.0`                       | Backoff base delay (s)                                 |
| `BLAZECRAWL_RESPECT_ROBOTS_BY_DEFAULT` | `true`                   | Default `respect_robots_txt` value                     |

### Note on list-valued env vars

Pydantic-settings expects JSON for complex env values. To set `PROXIES` or `BLOCK_RESOURCES` via the environment, use JSON-array syntax:

```bash
export BLAZECRAWL_PROXIES='["http://user:pass@proxy1:8080","http://proxy2:8080"]'
export BLAZECRAWL_BLOCK_RESOURCES='["image","font","media","stylesheet"]'
```

A comma-separated form also works when settings are constructed directly in Python (the model includes pre-validators for that path), but JSON is the safe canonical form for environment variables.

## Interactive docs

Once the server is running, browse to:

- Swagger UI → http://localhost:8000/docs
- ReDoc      → http://localhost:8000/redoc

## License

MIT
