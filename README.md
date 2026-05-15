# One Journal A Day

A small personal project I built to keep up with research.

Live site: **[abhijeet-and-data.github.io/daily-paper](https://github.com/bow-pow/daily-paper)** *(update with your actual URL)*
LinkedIn: **[Abhijeet Sharma](https://www.linkedin.com/in/abhijeet-and-data/)**

---

## What it does

Every morning at 6 AM IST, this site picks one research paper from arXiv,
explains it in plain English, and reads it aloud in a natural voice. I built
it because I wanted a low-friction way to follow what's happening in physics,
math, AI, and adjacent fields without drowning in papers or settling for
hype-driven newsletters.

Topics rotate through the week — physics on Monday, astrophysics Tuesday,
math Wednesday, AI Thursday, ML Friday, quantum Saturday, condensed matter
Sunday. Each day the script casts a wide net across the last 3-5 years of
that topic, scores every candidate by citation impact via Semantic Scholar,
filters out papers I've already been shown, and picks the highest-rated one
that's new to me.

Then it asks Google Gemini to write two summaries — a 150-word brief for the
landing page, and a deeper section-by-section explainer based on the full PDF —
and generates an MP3 narration using Microsoft's Ava neural voice via edge-tts.

The whole pipeline runs as a GitHub Action and publishes to GitHub Pages.
No server, no database, no per-user accounts. Total monthly cost: $0.

---

## What's on the page

- **The Brief** — a 150-word plain-English summary of today's paper
- **Listen button** — plays a natural-voice MP3 of the brief
- **Favorite button** — bookmark a paper to revisit (stored locally in browser)
- **Read the full-paper summary** — expandable deeper explainer
- **Links** to the original arXiv abstract and PDF
- **Archive** of every past day's paper
- **Favorites page** listing everything I've starred

The design aims for a quiet reading experience rather than a tech dashboard —
serif body, restrained palette, drop cap on the brief. Works as a Progressive
Web App: add it to your phone's home screen and it opens like an app.

---

## Why I built it

Three reasons.

**1. Curiosity without obligation.** I wanted to stay current on multiple
research fields without committing to a literature-review schedule or
relying on Twitter/LinkedIn for technical material.

**2. Plain-English explanations.** Most papers are written for specialists,
and most science news loses the substance. I wanted a middle ground — actual
research, but explained accessibly enough that I could read it before coffee.

**3. Build something small and complete.** I like projects that have a
beginning, middle, and end. This one is small enough to fully understand,
free to operate forever, and useful every day.

---

## Stack

| Layer | Tool | Why |
| --- | --- | --- |
| Paper source | arXiv API + RSS fallback | Free, open, comprehensive |
| Ranking | Semantic Scholar batch API | Free citation data |
| Summarization | Google Gemini 2.5 Flash-Lite | Free tier covers daily use |
| Narration | edge-tts (Microsoft neural) | Free, no API key |
| Build & hosting | GitHub Actions + Pages | Free, no maintenance |
| Frontend | Static HTML/CSS/JS, no framework | Zero dependencies, instant load |

---

## Setup (if you want your own copy)

### 1. Get a free Gemini API key
- Go to <https://aistudio.google.com/apikey>, sign in, click **Create API key**.
- No credit card needed.

### 2. Fork or clone this repo
- Make it your own public GitHub repo.

### 3. Add your API key as a repo secret
- **Settings → Secrets and variables → Actions → New repository secret**.
- Name: `GEMINI_API_KEY`. Value: the key from step 1.

### 4. Enable GitHub Pages
- **Settings → Pages → Source: GitHub Actions**.

### 5. Run the first build
- **Actions → Daily Paper Build → Run workflow**.
- After ~2 minutes the deploy step prints your public URL.

---

## Local testing

```bash
pip install pypdf edge-tts
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
| Lookback window | `scripts/build.py` → `years_back_for()` | Adjust per-topic age cap |
| Build time | `.github/workflows/daily.yml` → `cron` | Standard cron in UTC |
| Brief style | `scripts/build.py` → `BRIEF_PROMPT` | Reword for tone, length, audience |
| Deep summary depth | `scripts/build.py` → `DEEP_PROMPT` | Add/remove sections |
| Voice | `scripts/build.py` → `TTS_VOICE` | e.g. `en-US-AndrewNeural`, `en-GB-SoniaNeural` |
| Visual style | `scripts/template.html` | All CSS is in this one file |

---

## Reading on your phone

Open the site URL in mobile Safari/Chrome and **Add to Home Screen**.
It opens like a native app and works offline once cached.

---

## Acknowledgements

- [arXiv](https://arxiv.org) for the open paper archive
- [Allen Institute for AI / Semantic Scholar](https://www.semanticscholar.org) for citation data
- [Google AI Studio](https://aistudio.google.com) for the Gemini free tier
- [edge-tts](https://github.com/rany2/edge-tts) for free neural narration
- [GitHub](https://github.com) for free Actions runtime and Pages hosting

---

Built by [Abhijeet Sharma](https://www.linkedin.com/in/abhijeet-and-data/).
