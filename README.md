# Daily Paper

A tiny, free, self-hosted "research paper of the day" site.

Every morning at 07:00 UTC, a GitHub Action picks one recent paper from arXiv
(rotating through physics, astro-ph, math, cs.AI, cs.LG, quant-ph, cond-mat),
asks Google Gemini to write a plain-English brief, and then a deeper section-by-section
summary based on the full PDF. The page is committed to this repo and served by
GitHub Pages.

Total monthly cost: **$0** (free GitHub Actions minutes + Gemini free tier + GitHub Pages).

---

## Setup (one-time, ~15 minutes)

### 1. Get a free Gemini API key
- Go to <https://aistudio.google.com/apikey>, sign in, click **Create API key**.
- No credit card needed. The free tier easily covers two short calls per day.

### 2. Create a GitHub repo
- Create a new **public** repo (private also works, but Pages costs nothing on public).
- Push these files to it:
  ```
  .github/workflows/daily.yml
  scripts/build.py
  scripts/build_archive_index.py
  scripts/template.html
  site/                # empty; will be populated by the first run
  ```

### 3. Add your API key as a repo secret
- Go to **Settings → Secrets and variables → Actions → New repository secret**.
- Name: `GEMINI_API_KEY`. Value: the key from step 1.

### 4. Enable GitHub Pages
- Go to **Settings → Pages**.
- Under **Build and deployment → Source**, pick **GitHub Actions**.
- The workflow already handles deployment; no extra config needed.

### 5. Trigger the first build
- Go to **Actions → Daily Paper Build → Run workflow**.
- After ~2 minutes the job finishes; the deploy step prints your public URL,
  something like `https://<your-username>.github.io/<repo-name>/`.

That's it. From then on it runs itself every morning.

---

## Local testing

If you want to try a run on your own machine before pushing:

```bash
pip install pypdf
export GEMINI_API_KEY=your-key-here
python scripts/build.py
python scripts/build_archive_index.py
open site/index.html
```

---

## Customizing

| What | Where | How |
| --- | --- | --- |
| Topic rotation | `scripts/build.py` → `WEEKLY_TOPICS` | Change arXiv categories per weekday |
| Build time | `.github/workflows/daily.yml` → `cron` | Standard cron in UTC |
| Brief style | `scripts/build.py` → `BRIEF_PROMPT` | Reword for tone, length, audience |
| Deep summary depth | `scripts/build.py` → `DEEP_PROMPT` | Add/remove sections |
| Visual style | `scripts/template.html` | All CSS is in this one file |
| Model | `scripts/build.py` → `GEMINI_MODEL` | e.g. `gemini-2.0-flash` (fast) |

---

## Reading on your phone

Just open the GitHub Pages URL in mobile Safari/Chrome and **Add to Home Screen**.
It opens like a native app and works offline once cached.

---

## Why this design

- **Static site** — no server to pay for, can't crash, opens instantly.
- **arXiv API** — free, no key, no rate-limit concerns at one call per day.
- **Gemini free tier** — two calls per day stays inside free quotas comfortably.
- **GitHub Actions cron** — public repos get 2000 free minutes/month; daily uses ~2.
- **Deterministic pick per day** — re-running the workflow on the same date gives
  the same paper, so reruns don't surprise you.
