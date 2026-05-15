"""
Daily Paper builder.

Fetches one recent arXiv paper from a rotating category, asks Gemini for
two summaries (a short plain-English brief, plus a deeper section-by-section
explainer based on the full PDF text), and writes site/index.html.

Designed to be run by GitHub Actions on a daily cron.
"""

from __future__ import annotations

import io
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# --- Configuration -----------------------------------------------------------

# Topic rotation: index by weekday (Monday=0). Tweak freely.
# arXiv category reference: https://arxiv.org/category_taxonomy
WEEKLY_TOPICS = {
    0: ("physics", "physics.gen-ph OR physics.flu-dyn OR physics.optics"),
    1: ("astro-ph", "astro-ph.GA OR astro-ph.CO OR astro-ph.HE OR astro-ph.SR"),
    2: ("math",    "math.PR OR math.NT OR math.AG OR math.CO OR math.DG"),
    3: ("cs.AI",   "cs.AI"),
    4: ("cs.LG",   "cs.LG"),
    5: ("quant-ph","quant-ph"),
    6: ("cond-mat","cond-mat.stat-mech OR cond-mat.soft OR cond-mat.mes-hall"),
}

ARXIV_API = "https://export.arxiv.org/api/query"
USER_AGENT = "daily-paper/1.0 (https://github.com/; contact: github-actions)"
GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"
ARCHIVE_DIR = SITE_DIR / "archive"
TEMPLATE_PATH = ROOT / "scripts" / "template.html"
SEEN_PATH = ROOT / "seen.json"

SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper/batch"

# How far back to look for candidates, per topic. CS fields move fast and
# citations accumulate quickly, so 3 years is plenty. Math/physics/astro
# citations build more slowly, so we widen the window to 5 years.
def years_back_for(topic_label: str) -> int:
    return 3 if topic_label.startswith("cs.") else 5


# --- Data model --------------------------------------------------------------

@dataclass
class Paper:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    categories: list[str]
    published: str       # ISO date
    pdf_url: str
    abs_url: str


# --- arXiv fetching ----------------------------------------------------------

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _http_get(url: str, timeout: int = 30, attempts: int = 5) -> bytes:
    """GET with exponential backoff. Handles transient timeouts/5xx/429 from arXiv.

    Backoff is aggressive: arXiv's API rate-limits to ~1 req / 3s and is happiest
    with patient clients. We also honour the Retry-After header on 429.
    """
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_err = e
            # 429 = rate limited. Respect Retry-After or wait longer.
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                try:
                    wait = int(retry_after) if retry_after else 0
                except (TypeError, ValueError):
                    wait = 0
                wait = max(wait, 15 * (2 ** i))   # 15, 30, 60, 120, 240s
            elif 500 <= e.code < 600:
                wait = 5 * (2 ** i)
            else:
                # 4xx other than 429 won't fix itself — bail out fast.
                raise
            print(f"  attempt {i+1}/{attempts}: HTTP {e.code}; sleeping {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
        except Exception as e:
            last_err = e
            wait = 5 * (2 ** i)  # 5, 10, 20, 40, 80s
            print(f"  attempt {i+1}/{attempts} failed ({e!r}); sleeping {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"HTTP GET failed after {attempts} attempts: {last_err}")


def _fetch_via_api(category_query: str, max_results: int, years_back: int) -> list[Paper]:
    """Primary path: arXiv Atom API. Fetches up to `max_results` papers from
    the given category, submitted within the last `years_back` years.
    """
    now = datetime.now(timezone.utc)
    # arXiv submittedDate filter: YYYYMMDDHHMM in UTC
    start = now.replace(year=now.year - years_back).strftime("%Y%m%d") + "0000"
    end = now.strftime("%Y%m%d") + "2359"

    params = {
        "search_query": (
            f"cat:({category_query}) "
            f"AND submittedDate:[{start} TO {end}]"
        ),
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
    # arXiv asks clients to wait ~3s between requests; pause before the first one
    # to be a polite neighbour on shared cloud IPs.
    time.sleep(3)
    body = _http_get(url, timeout=45).decode("utf-8")
    return _parse_atom(body)


def _parse_atom(body: str) -> list[Paper]:
    root = ET.fromstring(body)
    papers: list[Paper] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        full_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS).strip()
        arxiv_id = full_id.rsplit("/", 1)[-1]
        bare_id = re.sub(r"v\d+$", "", arxiv_id)
        title = " ".join(
            entry.findtext("atom:title", default="", namespaces=ATOM_NS).split()
        )
        abstract = " ".join(
            entry.findtext("atom:summary", default="", namespaces=ATOM_NS).split()
        )
        published = entry.findtext("atom:published", default="", namespaces=ATOM_NS)[:10]
        authors = [
            a.findtext("atom:name", default="", namespaces=ATOM_NS).strip()
            for a in entry.findall("atom:author", ATOM_NS)
        ]
        categories = [
            c.get("term", "")
            for c in entry.findall("{http://arxiv.org/schemas/atom}category")
        ]
        if title and abstract:
            papers.append(Paper(
                arxiv_id=bare_id,
                title=title,
                authors=authors,
                abstract=abstract,
                categories=categories,
                published=published,
                pdf_url=f"https://arxiv.org/pdf/{bare_id}",
                abs_url=f"https://arxiv.org/abs/{bare_id}",
            ))
    return papers


def _fetch_via_rss(category_query: str) -> list[Paper]:
    """Fallback path: arXiv RSS feed (different infrastructure, more reliable from
    cloud IPs). RSS doesn't support boolean OR queries — we just pick the first
    listed category from category_query and use it alone.
    """
    primary_cat = category_query.split(" OR ")[0].strip().strip("()")
    url = f"https://rss.arxiv.org/rss/{primary_cat}"
    print(f"  fallback: trying RSS feed for {primary_cat}")
    body = _http_get(url, timeout=30, attempts=3).decode("utf-8")

    # Minimal RSS 2.0 / Atom-ish parser. Each <item> has <title>, <description>,
    # <link>, <dc:creator>, and a <guid> containing the arXiv id.
    root = ET.fromstring(body)
    # Handle both rss/channel/item and feed/entry shapes
    items = root.findall(".//item")
    papers: list[Paper] = []
    DC_NS = {"dc": "http://purl.org/dc/elements/1.1/"}
    for it in items:
        guid = (it.findtext("guid") or it.findtext("link") or "").strip()
        m = re.search(r"(\d{4}\.\d{4,5})", guid)
        if not m:
            continue
        bare_id = m.group(1)
        title = re.sub(r"\s+", " ", (it.findtext("title") or "")).strip()
        # arXiv RSS often prefixes title with category in brackets; clean it up.
        title = re.sub(r"^\[[^\]]+\]\s*", "", title)
        abstract_raw = it.findtext("description") or ""
        # Strip HTML tags and arxiv's "Abstract:" prefix
        abstract = re.sub(r"<[^>]+>", " ", abstract_raw)
        abstract = re.sub(r"\s+", " ", abstract).strip()
        abstract = re.sub(r"^Abstract:?\s*", "", abstract, flags=re.IGNORECASE)
        authors_text = it.findtext("dc:creator", default="", namespaces=DC_NS) or ""
        authors = [a.strip() for a in re.split(r",| and ", authors_text) if a.strip()]
        if title and abstract:
            papers.append(Paper(
                arxiv_id=bare_id,
                title=title,
                authors=authors,
                abstract=abstract,
                categories=[primary_cat],
                published=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                pdf_url=f"https://arxiv.org/pdf/{bare_id}",
                abs_url=f"https://arxiv.org/abs/{bare_id}",
            ))
    return papers


def fetch_candidates(
    category_query: str,
    max_results: int = 200,
    years_back: int = 3,
) -> list[Paper]:
    """Query arXiv for papers. Tries Atom API first, falls back to RSS.

    With citation-based ranking, we want a wide pool from a multi-year window,
    not just the last day. The RSS fallback only has yesterday's submissions,
    so on fallback the picker just picks recency.
    """
    try:
        papers = _fetch_via_api(category_query, max_results, years_back)
        if papers:
            return papers
        print("  API returned no results; trying RSS", file=sys.stderr)
    except Exception as e:
        print(f"  API failed ({e!r}); trying RSS", file=sys.stderr)
    return _fetch_via_rss(category_query)


# --- Semantic Scholar scoring ----------------------------------------------

def score_with_semantic_scholar(papers: list[Paper]) -> dict[str, dict]:
    """Look up citation data for each paper. Returns dict keyed by arxiv_id with
    fields {citations, influential, year, score}. Papers missing from the index
    get score=0. Failure to reach the API returns {} (caller falls back).

    Uses the batch endpoint which accepts up to 500 ids in one POST.
    """
    if not papers:
        return {}

    ids = [f"ARXIV:{p.arxiv_id}" for p in papers]
    payload = json.dumps({"ids": ids}).encode("utf-8")
    fields = "citationCount,influentialCitationCount,year,publicationDate,tldr"
    url = f"{SEMANTIC_SCHOLAR_API}?fields={fields}"

    req = urllib.request.Request(
        url, data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    # Semantic Scholar's free tier allows ~100 req / 5 min. Plenty for one
    # call per day, but the endpoint can rate-limit on shared cloud IPs.
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                results = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 20 * (2 ** attempt)  # 20, 40, 80
                print(f"  Semantic Scholar 429; sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  Semantic Scholar failed: {e}", file=sys.stderr)
            return {}
        except Exception as e:
            print(f"  Semantic Scholar failed: {e!r}", file=sys.stderr)
            if attempt < 3:
                time.sleep(5 * (2 ** attempt))
                continue
            return {}
    else:
        return {}

    # results is a list aligned with ids; entries can be None when SS doesn't
    # have the paper (e.g. very recent submissions).
    scores: dict[str, dict] = {}
    current_year = datetime.now(timezone.utc).year
    for paper, info in zip(papers, results):
        if not info:
            scores[paper.arxiv_id] = {"citations": 0, "influential": 0, "score": 0.0}
            continue
        citations = info.get("citationCount") or 0
        influential = info.get("influentialCitationCount") or 0
        year = info.get("year") or current_year
        age_years = max(1, current_year - year + 1)
        # Score: weight influential 3x raw, normalize by age so a 30-cite paper
        # from this year scores like a 90-cite paper from 3 years ago.
        # This gives "rising" recent papers a fair shot against established old ones.
        score = (citations + 3 * influential) / age_years
        tldr = (info.get("tldr") or {}).get("text") if info.get("tldr") else None
        scores[paper.arxiv_id] = {
            "citations": citations,
            "influential": influential,
            "year": year,
            "score": score,
            "tldr": tldr,
        }
    return scores


# --- Picking ---------------------------------------------------------------

def load_seen() -> set[str]:
    """Load the set of arxiv IDs we've already shown the user."""
    if not SEEN_PATH.exists():
        return set()
    try:
        data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        return set(data.get("ids", []))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    """Persist the seen set. Trimmed to 1000 entries (the most recent ones win)
    so the file doesn't grow forever.
    """
    ids = list(seen)
    if len(ids) > 1000:
        ids = ids[-1000:]
    SEEN_PATH.write_text(
        json.dumps({"ids": ids}, indent=2), encoding="utf-8"
    )


def pick_paper(
    papers: list[Paper],
    scores: dict[str, dict],
    seen: set[str],
    seed: str,
) -> tuple[Paper, dict | None]:
    """Pick the highest-scored paper that the user hasn't been shown before.

    Returns (paper, score_info). score_info is None if scoring was unavailable
    or empty for this paper, in which case the caller knows it's a fallback pick.

    If every paper has been seen (very rare — we keep 1000 in seen.json so this
    only happens after >1000 days), fall back to a deterministic random pick.
    """
    # Filter out already-seen papers
    fresh = [p for p in papers if p.arxiv_id not in seen]
    if not fresh:
        print("  all candidates seen; ignoring seen list", file=sys.stderr)
        fresh = papers

    if scores:
        # Sort by score descending, breaking ties by recency (papers come in
        # newest-first from arXiv, so stable sort preserves that)
        ranked = sorted(fresh, key=lambda p: scores.get(p.arxiv_id, {}).get("score", 0), reverse=True)
        top = ranked[0]
        info = scores.get(top.arxiv_id)
        # If the top-scored paper has zero citations, the scoring didn't help
        # us — log it but still return the pick (it's still a recent paper).
        if info and info.get("score", 0) > 0:
            print(f"  top score: {info['citations']} citations "
                  f"({info['influential']} influential), score={info['score']:.1f}")
        else:
            print("  top candidate has no citation data — likely too recent")
        return top, info

    # No scoring at all (Semantic Scholar unreachable). Deterministic random
    # from the top of the recency list, same as the original behaviour.
    rng = random.Random(seed)
    pool = fresh[:25] or fresh
    return rng.choice(pool), None


# --- PDF text extraction -----------------------------------------------------

def fetch_pdf_text(pdf_url: str) -> str:
    """Download a PDF and extract its text. Returns '' on failure."""
    try:
        import pypdf  # type: ignore
    except ImportError:
        print("pypdf not installed; skipping full-text extraction", file=sys.stderr)
        return ""

    try:
        data = _http_get(pdf_url, timeout=60, attempts=3)
    except Exception as e:
        print(f"PDF download failed: {e}", file=sys.stderr)
        return ""

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(chunks)
        # Collapse whitespace; arXiv PDFs are messy
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Cap length to keep Gemini call cheap and fast
        return text[:60_000]
    except Exception as e:
        print(f"PDF parse failed: {e}", file=sys.stderr)
        return ""


# --- Gemini summarization ----------------------------------------------------

BRIEF_PROMPT = """You are explaining a research paper to a curious, intelligent reader who is NOT a specialist in this field.

Paper title: {title}
Authors: {authors}
arXiv category: {category}
Abstract:
{abstract}

Write a short brief (about 150-200 words) with this structure:
1. One sentence stating, in plain English, what the paper is about.
2. 2-3 sentences explaining the key idea or finding, defining any necessary jargon inline.
3. One sentence on why a non-specialist might find this interesting or what it could matter for.

Avoid hedging, avoid the phrases "this paper" or "the authors", be direct and concrete. Do not use Markdown headings. Plain prose only."""

DEEP_PROMPT = """You are writing a deeper explainer of a research paper for a curious, intelligent reader who is NOT a specialist in this field. They've already read a short brief and want to understand the paper more thoroughly.

Paper title: {title}
Authors: {authors}
arXiv category: {category}

Abstract:
{abstract}

Full paper text (may be truncated and OCR-noisy):
{fulltext}

Write a 500-700 word explainer with these sections, each on its own line prefixed with the section name in bold Markdown (e.g. **Background**):

**Background** — what problem or question motivated this work, in plain language.
**What they did** — the method, approach, or argument, with jargon defined inline.
**What they found** — the main results, stated concretely. Use specific numbers where the paper does.
**Why it matters** — implications, limitations, and what a non-specialist should take away.

Be direct and concrete. Do not say "this paper" or "the authors" — talk about the ideas directly. Do not invent details that aren't in the source. If the full text is missing or unhelpful, rely on the abstract and say less rather than guessing."""


def call_gemini(prompt: str, api_key: str) -> str:
    """Send a single prompt to Gemini and return the text response."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 1500,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    url = f"{GEMINI_ENDPOINT}?key={api_key}"

    last_err: Exception | None = None
    for attempt in range(5):
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read().decode("utf-8"))
            return resp["candidates"][0]["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as e:
            last_err = e
            # Try to parse RetryInfo from Gemini's error body
            body_wait = 0
            try:
                err_body = json.loads(e.read().decode("utf-8"))
                for detail in err_body.get("error", {}).get("details", []):
                    if detail.get("@type", "").endswith("RetryInfo"):
                        delay = detail.get("retryDelay", "")
                        m = re.match(r"(\d+)s", delay)
                        if m:
                            body_wait = int(m.group(1))
            except Exception:
                pass

            if e.code == 429:
                wait = max(body_wait, 30 * (2 ** attempt))  # 30, 60, 120, 240, 480s
            elif 500 <= e.code < 600:
                wait = 5 * (2 ** attempt)
            else:
                # 400/401/403 etc. won't fix themselves — fail fast.
                raise
            print(f"  Gemini attempt {attempt+1}/5: HTTP {e.code}; sleeping {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
        except Exception as e:
            last_err = e
            wait = 5 * (2 ** attempt)
            print(f"  Gemini attempt {attempt+1}/5 failed ({e!r}); sleeping {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Gemini call failed after retries: {last_err}")


# --- HTML rendering ----------------------------------------------------------

def escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def render_deep_summary_html(text: str) -> str:
    """Convert the **Section** markdown headings into proper HTML."""
    # Split on bold-section markers
    parts = re.split(r"\*\*([^*]+)\*\*", text)
    # parts looks like: [pre_text, 'Section1', body1, 'Section2', body2, ...]
    chunks: list[str] = []
    if parts and parts[0].strip():
        chunks.append(f"<p>{escape(parts[0].strip())}</p>")
    for i in range(1, len(parts), 2):
        heading = escape(parts[i].strip().rstrip("—-: "))
        body = escape(parts[i + 1].strip()) if i + 1 < len(parts) else ""
        # Break body on blank lines into paragraphs
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        body_html = "".join(f"<p>{p}</p>" for p in paras) if paras else ""
        chunks.append(f'<h3>{heading}</h3>{body_html}')
    return "\n".join(chunks)


def escape_js(s: str) -> str:
    """Escape a string for safe embedding inside a JS string literal in HTML.
    Uses json.dumps for correct escaping, strips its outer quotes, and also
    breaks any '</script' sequence that could otherwise close our script tag.
    """
    inner = json.dumps(s, ensure_ascii=False)[1:-1]
    # Defend against accidental script-tag closure in the title
    return inner.replace("</", "<\\/")


# --- Text-to-speech via edge-tts --------------------------------------------

# Voice options worth trying: en-US-AvaNeural (warm, neutral),
# en-US-AndrewNeural (calm male), en-GB-SoniaNeural (British female),
# en-US-EmmaNeural (animated). Full list: `edge-tts --list-voices`.
TTS_VOICE = "en-US-AvaNeural"
TTS_RATE = "-5%"   # slight slowdown for easier comprehension


def _strip_for_speech(text: str) -> str:
    """Clean up Markdown markers and other artefacts so the voice reads cleanly.
    edge-tts speaks punctuation literally, so we want plain prose only.
    """
    s = text
    # Bold/italic markers
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\*(.+?)\*", r"\1", s)
    s = re.sub(r"__(.+?)__", r"\1", s)
    s = re.sub(r"_(.+?)_", r"\1", s)
    # Inline code and links
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    # Section dashes/em-dashes after a heading — keep but normalize
    s = s.replace("—", ", ")
    # Collapse whitespace
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n\n", s).strip()
    return s


def synthesize_brief_audio(brief_text: str, out_path: Path) -> bool:
    """Generate an MP3 of the brief at out_path. Returns True on success.

    Failure is non-fatal — the page falls back to browser TTS.
    """
    try:
        import asyncio
        import edge_tts  # type: ignore
    except ImportError:
        print("edge-tts not installed; skipping audio synthesis", file=sys.stderr)
        return False

    speech_text = _strip_for_speech(brief_text)
    if not speech_text:
        return False

    async def _run() -> None:
        communicate = edge_tts.Communicate(
            text=speech_text,
            voice=TTS_VOICE,
            rate=TTS_RATE,
        )
        await communicate.save(str(out_path))

    # Retry a couple of times — the unofficial endpoint occasionally hiccups.
    for attempt in range(3):
        try:
            asyncio.run(_run())
            if out_path.exists() and out_path.stat().st_size > 1024:
                return True
            print(f"  TTS attempt {attempt+1}: file looked empty, retrying", file=sys.stderr)
        except Exception as e:
            print(f"  TTS attempt {attempt+1} failed: {e!r}", file=sys.stderr)
            time.sleep(2 ** attempt)
    return False


def render_page(
    paper: Paper, brief: str, deep_html: str,
    generated_at: str, today_label: str, audio_url: str = ""
) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    authors_str = ", ".join(paper.authors[:6]) + (" et al." if len(paper.authors) > 6 else "")
    cats_str = ", ".join(paper.categories[:4])

    brief_paragraphs = "".join(
        f"<p>{escape(p.strip())}</p>"
        for p in re.split(r"\n\s*\n", brief.strip())
        if p.strip()
    )

    return (template
        .replace("{{TITLE}}", escape(paper.title))
        .replace("{{TITLE_JS}}", escape_js(paper.title))
        .replace("{{AUTHORS}}", escape(authors_str))
        .replace("{{CATEGORIES}}", escape(cats_str))
        .replace("{{PUBLISHED}}", escape(paper.published))
        .replace("{{ARXIV_ID}}", escape(paper.arxiv_id))
        .replace("{{ABS_URL}}", escape(paper.abs_url))
        .replace("{{PDF_URL}}", escape(paper.pdf_url))
        .replace("{{BRIEF_HTML}}", brief_paragraphs)
        .replace("{{DEEP_HTML}}", deep_html)
        .replace("{{GENERATED_AT}}", escape(generated_at))
        .replace("{{TODAY_LABEL}}", escape(today_label))
        .replace("{{AUDIO_URL}}", escape_js(audio_url))
    )


# --- Main --------------------------------------------------------------------

def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY env var not set", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    weekday = now.weekday()
    topic_label, category_query = WEEKLY_TOPICS[weekday]
    today_iso = now.strftime("%Y-%m-%d")
    today_label = now.strftime("%A, %B %-d, %Y")
    generated_at = now.strftime("%Y-%m-%d %H:%M UTC")

    years = years_back_for(topic_label)
    print(f"[{today_iso}] Topic: {topic_label} ({category_query}); window: last {years} years")

    candidates = fetch_candidates(category_query, years_back=years)
    if not candidates:
        print("No candidates returned; aborting", file=sys.stderr)
        return 1
    print(f"Got {len(candidates)} candidates")

    print("Scoring with Semantic Scholar...")
    scores = score_with_semantic_scholar(candidates)
    if scores:
        scored = sum(1 for s in scores.values() if s.get("score", 0) > 0)
        print(f"  scored {scored}/{len(candidates)} papers (others had no citations yet)")
    else:
        print("  scoring unavailable; will fall back to random pick")

    seen = load_seen()
    paper, score_info = pick_paper(candidates, scores, seen, seed=today_iso)
    print(f"Picked: {paper.arxiv_id} — {paper.title}")

    # Record this pick so we don't repeat it next time
    seen.add(paper.arxiv_id)
    save_seen(seen)

    print("Generating brief summary...")
    brief = call_gemini(
        BRIEF_PROMPT.format(
            title=paper.title,
            authors=", ".join(paper.authors),
            category=topic_label,
            abstract=paper.abstract,
        ),
        api_key,
    )

    print("Downloading PDF for deep summary...")
    fulltext = fetch_pdf_text(paper.pdf_url)
    if fulltext:
        print(f"  extracted {len(fulltext):,} chars")
    else:
        print("  no full text; deep summary will use abstract only")

    print("Generating deep summary...")
    deep_raw = call_gemini(
        DEEP_PROMPT.format(
            title=paper.title,
            authors=", ".join(paper.authors),
            category=topic_label,
            abstract=paper.abstract,
            fulltext=fulltext or "(full text unavailable; rely on abstract)",
        ),
        api_key,
    )
    deep_html = render_deep_summary_html(deep_raw)

    # Generate spoken-audio version of the brief. Non-fatal if it fails.
    print("Synthesizing audio...")
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = SITE_DIR / "today.mp3"
    audio_ok = synthesize_brief_audio(brief, audio_path)
    if audio_ok:
        # Archive a per-date copy so old days keep their audio
        archive_audio = ARCHIVE_DIR / f"{today_iso}.mp3"
        archive_audio.write_bytes(audio_path.read_bytes())
        size_kb = audio_path.stat().st_size // 1024
        print(f"  wrote site/today.mp3 ({size_kb} KB) and archive/{today_iso}.mp3")
    else:
        # Stale audio from a previous day would mismatch today's text.
        if audio_path.exists():
            audio_path.unlink()
        print("  audio synthesis failed; page will fall back to browser TTS")

    # Render index.html — references today.mp3 (latest audio)
    html_index = render_page(
        paper, brief, deep_html, generated_at, today_label,
        audio_url="today.mp3" if audio_ok else "",
    )
    (SITE_DIR / "index.html").write_text(html_index, encoding="utf-8")

    # Render the archived copy — references its dated audio file
    html_archive = render_page(
        paper, brief, deep_html, generated_at, today_label,
        audio_url=f"{today_iso}.mp3" if audio_ok else "",
    )
    (ARCHIVE_DIR / f"{today_iso}.html").write_text(html_archive, encoding="utf-8")

    # Ship the favorites page alongside the index. It's static and the same
    # every day; just copy it from the scripts/ directory.
    favs_src = ROOT / "scripts" / "favorites_page.html"
    if favs_src.exists():
        (SITE_DIR / "favorites.html").write_text(
            favs_src.read_text(encoding="utf-8"), encoding="utf-8"
        )

    print(f"Wrote site/index.html and archive/{today_iso}.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
