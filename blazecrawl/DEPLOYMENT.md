# BlazeCrawl Deployment Guide

BlazeCrawl needs a **long-running container** (Playwright Chromium + background
crawl jobs + in-memory cache). This rules out Vercel, Netlify Functions, AWS
Lambda, and Cloudflare Workers. Below are platforms that work, easiest first.

---

## 1. Railway (recommended for ease)

The closest "git push and deploy" experience to Vercel, but for persistent
containers.

### Step 1 — Push to GitHub

```bash
cd blazecrawl
git init
git add .
git commit -m "Initial BlazeCrawl commit"
gh repo create blazecrawl --public --source=. --push
# or create the repo manually at github.com/new and push
```

### Step 2 — Deploy on Railway

1. Sign up at <https://railway.app> (GitHub login works)
2. Click **New Project → Deploy from GitHub repo → blazecrawl**
3. Railway auto-detects the `Dockerfile`, builds, and deploys (first build:
   2–3 minutes — Chromium is large)
4. Once deployed, go to **Settings → Networking → Generate Domain**

You now have a public URL like `https://blazecrawl-production.up.railway.app`.

### Step 3 — Set environment variables (optional)

In **Variables**, add anything from the env-var table in the main README:

```
BLAZECRAWL_LOG_LEVEL=INFO
BLAZECRAWL_DEFAULT_RPS=2.0
BLAZECRAWL_PROXIES=http://user:pass@proxy.example.com:8080
```

### Step 4 — Test

```bash
curl https://YOUR-DOMAIN.up.railway.app/health
curl -X POST https://YOUR-DOMAIN.up.railway.app/scrape \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

### Pricing

Hobby plan is $5/month and includes $5 of usage. BlazeCrawl idles at roughly
$3–8/month, climbs with traffic.

---

## 2. Render

Similar to Railway, with a free tier (but free services spin down after 15 min
of inactivity, and the cold start to launch Chromium is slow — not great for
production).

1. Push to GitHub (see above)
2. Sign up at <https://render.com>
3. **New → Web Service → Connect GitHub → pick repo**
4. Settings:
   - **Environment**: Docker
   - **Region**: closest to your users
   - **Plan**: Starter ($7/mo) or Free
5. **Create Web Service**

Render auto-detects the Dockerfile and the `$PORT` env var.

---

## 3. Fly.io

More CLI-driven, good if you want multi-region or lower latency.

```bash
# install: https://fly.io/docs/getting-started/installing-flyctl/
fly auth signup
cd blazecrawl
fly launch          # answer prompts; pick a region
fly deploy
fly open
```

Fly auto-detects the Dockerfile. Free allowance covers small workloads.

---

## 4. DigitalOcean App Platform

Managed but more expensive than Railway/Render.

1. Push to GitHub
2. <https://cloud.digitalocean.com/apps> → **Create App → GitHub → pick repo**
3. Detects the Dockerfile automatically
4. Choose Basic plan ($5/mo for the smallest instance — but Chromium needs at
   least 1 GB RAM, so realistically $12/mo for the next tier up)

---

## 5. Plain VPS with Docker (cheapest at any real traffic)

A $5/month DigitalOcean droplet or Hetzner CX21 (~€4/mo) with 2 GB RAM is
plenty for moderate traffic.

```bash
# On your VPS (Ubuntu 22.04+)
curl -fsSL https://get.docker.com | sh

# Clone or scp the blazecrawl folder over, then:
cd blazecrawl
docker build -t blazecrawl .
docker run -d \
  --name blazecrawl \
  --restart unless-stopped \
  -p 8000:8000 \
  -e BLAZECRAWL_LOG_LEVEL=INFO \
  blazecrawl
```

Add Caddy or nginx in front for HTTPS:

```bash
# /etc/caddy/Caddyfile — auto-HTTPS via Let's Encrypt
your-domain.com {
    reverse_proxy localhost:8000
}
```

---

## Local testing of the Docker image

Before deploying anywhere:

```bash
cd blazecrawl
docker build -t blazecrawl .
docker run -p 8000:8000 blazecrawl
# Open http://localhost:8000/docs
```

If the build works locally, it'll work on any of the platforms above.

---

## What about Vercel / Netlify / Lambda?

These platforms run **short-lived serverless functions**, which is incompatible
with BlazeCrawl's design:

- Playwright Chromium (~170 MB) exceeds bundle size limits
- Background crawl jobs need a process that outlives the response
- The in-memory cache, rate limiter, and job tracker require a persistent
  process
- Single-request execution time caps (10–60s) are too short for many scrapes

You'd have to delete most of the project to fit. Use one of the
container-based platforms above instead.
