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
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
USER_AGENT = "OneJournalADay/1.0 (https://www.linkedin.com/in/abhijeet-and-data/; personal-research-reader)"
GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"
ARCHIVE_DIR = SITE_DIR / "archive"
TEMPLATE_PATH = ROOT / "scripts" / "template.html"
SEEN_PATH = ROOT / "seen.json"
CACHE_DIR = ROOT / "candidates_cache"
CACHE_MAX_AGE_DAYS = 7   # Reuse a cached candidate pool for up to a week.

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
            # 429 = rate limited. arXiv per-IP blocks usually last hours; retrying
            # inside one run just wastes time and digs the hole deeper. Try once
            # with a short wait, then give up so the caller can fall back to cache.
            if e.code == 429:
                if i == 0:
                    retry_after = e.headers.get("Retry-After")
                    try:
                        wait = int(retry_after) if retry_after else 15
                    except (TypeError, ValueError):
                        wait = 15
                    wait = min(wait, 30)  # cap at 30s even if the server asks more
                    print(f"  attempt {i+1}/{attempts}: HTTP 429; sleeping {wait}s",
                          file=sys.stderr)
                    time.sleep(wait)
                    continue
                # Second 429 means we're in the penalty box. Bail.
                print(f"  attempt {i+1}/{attempts}: HTTP 429 again; giving up",
                      file=sys.stderr)
                raise
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


def _fetch_via_api(
    category_query: str,
    max_results: int,
    start_date: datetime,
    end_date: datetime,
) -> list[Paper]:
    """Primary path: arXiv Atom API. Fetches up to `max_results` papers from
    the given category, submitted between start_date and end_date.
    """
    start = start_date.strftime("%Y%m%d") + "0000"
    end = end_date.strftime("%Y%m%d") + "2359"

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
    # arXiv asks clients to wait ~3s between requests; pause before each one
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
    years_back: int = 3,
    per_slice: int = 30,
) -> list[Paper]:
    """Query arXiv for papers across a multi-year window. To make sure the
    candidate pool actually spans the whole window (rather than just the most
    recent few weeks of a fast-moving category), we issue several queries each
    over a time slice and combine the results.

    For a 3-year window we slice into 4 buckets:
      - last 0-3 months  (truly recent)
      - last 3-12 months (well-developed)
      - last 1-2 years   (peak citation age)
      - last 2-3 years   (older, well-cited classics for this window)
    For a 5-year window we add a fifth slice covering years 3-5.

    Returns up to ~per_slice * num_slices papers, deduplicated by arxiv_id.
    """
    now = datetime.now(timezone.utc)
    # Bucket boundaries in months back from now
    if years_back >= 5:
        bucket_months = [(0, 3), (3, 12), (12, 24), (24, 36), (36, 60)]
    else:  # 3
        bucket_months = [(0, 3), (3, 12), (12, 24), (24, 36)]

    all_papers: list[Paper] = []
    seen_ids: set[str] = set()
    consecutive_failures = 0
    rate_limited = False

    for newer, older in bucket_months:
        end = now - timedelta(days=newer * 30)
        start = now - timedelta(days=older * 30)
        try:
            chunk = _fetch_via_api(category_query, per_slice, start, end)
            consecutive_failures = 0
        except urllib.error.HTTPError as e:
            print(f"  slice {newer}-{older}mo: HTTP {e.code}", file=sys.stderr)
            if e.code == 429:
                rate_limited = True
            consecutive_failures += 1
            if consecutive_failures >= 2:
                print("  too many failures; abandoning Atom API for this run",
                      file=sys.stderr)
                break
            continue
        except Exception as e:
            print(f"  slice {newer}-{older}mo failed: {e!r}", file=sys.stderr)
            consecutive_failures += 1
            if consecutive_failures >= 2:
                print("  too many failures; abandoning Atom API for this run",
                      file=sys.stderr)
                break
            continue
        added = 0
        for p in chunk:
            if p.arxiv_id not in seen_ids:
                seen_ids.add(p.arxiv_id)
                all_papers.append(p)
                added += 1
        print(f"  slice {newer}-{older}mo: +{added} (got {len(chunk)})")

    if all_papers:
        return all_papers

    # Atom API returned nothing usable (empty, or fully blocked). Try RSS
    # which lives on different infrastructure and is rarely rate-limited.
    if rate_limited:
        print("  Atom API is rate-limiting us; falling back to RSS", file=sys.stderr)
    else:
        print("  Atom API returned no candidates; trying RSS fallback", file=sys.stderr)
    return _fetch_via_rss(category_query)


# --- Semantic Scholar scoring ----------------------------------------------

# Modern arXiv IDs are YYMM.NNNNN (4-5 digits after the dot). We exclude older
# format (e.g. hep-th/0101001) to keep request shape clean for the batch API.
ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}$")

# Chunk size for the batch endpoint. Semantic Scholar accepts up to 500 per
# request, but smaller chunks isolate failures (one bad ID in a chunk only
# loses the chunk, not all scores) and are easier on shared rate limits.
SS_CHUNK_SIZE = 100


def _ss_request_batch(ids: list[str]) -> list[dict | None] | None:
    """POST one batch of IDs. Returns the list result or None on failure."""
    if not ids:
        return []
    payload = json.dumps({"ids": ids}).encode("utf-8")
    fields = "citationCount,influentialCitationCount,year,publicationDate,tldr"
    url = f"{SEMANTIC_SCHOLAR_API}?fields={fields}"

    for attempt in range(4):
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 429 from Semantic Scholar: one short retry, then bail. Long
            # backoffs in a single run just waste time and dig us deeper.
            if e.code == 429:
                if attempt == 0:
                    print(f"    chunk: 429; sleeping 15s", file=sys.stderr)
                    time.sleep(15)
                    continue
                print(f"    chunk: 429 again; giving up on Semantic Scholar",
                      file=sys.stderr)
                return None
            # Read error body for diagnostic info on 400s
            try:
                err_body = e.read().decode("utf-8")[:200]
                print(f"    chunk: HTTP {e.code}: {err_body}", file=sys.stderr)
            except Exception:
                print(f"    chunk: HTTP {e.code}", file=sys.stderr)
            return None
        except Exception as e:
            if attempt < 3:
                time.sleep(5 * (2 ** attempt))
                continue
            print(f"    chunk: {e!r}", file=sys.stderr)
            return None
    return None


def score_with_semantic_scholar(papers: list[Paper]) -> dict[str, dict]:
    """Look up citation data for each paper. Returns dict keyed by arxiv_id with
    fields {citations, influential, year, score, tldr}.

    - Filters out arXiv IDs that don't match the modern YYMM.NNNNN format
      (old hep-th-style IDs were causing whole-batch 400 errors).
    - Chunks requests to isolate failures.
    - Papers missing from the index get score=0; papers not found in any
      successful chunk also get score=0 by default.
    """
    if not papers:
        return {}

    scores: dict[str, dict] = {}
    current_year = datetime.now(timezone.utc).year

    # Initialise every paper with a zero score so callers always get a value
    for p in papers:
        scores[p.arxiv_id] = {"citations": 0, "influential": 0, "score": 0.0}

    # Filter for modern arXiv IDs only
    valid = [p for p in papers if ARXIV_ID_RE.match(p.arxiv_id)]
    skipped = len(papers) - len(valid)
    if skipped:
        print(f"  skipped {skipped} papers with legacy-format arXiv IDs", file=sys.stderr)
    if not valid:
        return scores

    # Process in chunks
    n_chunks = (len(valid) + SS_CHUNK_SIZE - 1) // SS_CHUNK_SIZE
    successful_chunks = 0
    consecutive_failures = 0
    for i in range(0, len(valid), SS_CHUNK_SIZE):
        chunk = valid[i:i + SS_CHUNK_SIZE]
        ids = [f"ARXIV:{p.arxiv_id}" for p in chunk]
        results = _ss_request_batch(ids)
        if results is None:
            print(f"  chunk {i // SS_CHUNK_SIZE + 1}/{n_chunks}: failed", file=sys.stderr)
            consecutive_failures += 1
            if consecutive_failures >= 2:
                print("  Semantic Scholar appears to be blocking; abandoning scoring",
                      file=sys.stderr)
                break
            continue
        consecutive_failures = 0
        successful_chunks += 1
        for paper, info in zip(chunk, results):
            if not info:
                continue
            citations = info.get("citationCount") or 0
            influential = info.get("influentialCitationCount") or 0
            year = info.get("year") or current_year
            age_years = max(1, current_year - year + 1)
            score = (citations + 3 * influential) / age_years
            tldr = (info.get("tldr") or {}).get("text") if info.get("tldr") else None
            scores[paper.arxiv_id] = {
                "citations": citations,
                "influential": influential,
                "year": year,
                "score": score,
                "tldr": tldr,
            }
        # Be a polite neighbour between chunks
        if i + SS_CHUNK_SIZE < len(valid):
            time.sleep(2)

    if successful_chunks == 0:
        print("  no chunks succeeded; treating as scoring-unavailable", file=sys.stderr)
        return {}

    print(f"  successful chunks: {successful_chunks}/{n_chunks}")
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


# --- Candidate caching ------------------------------------------------------

def _cache_path(topic_label: str) -> Path:
    """Cache filename per topic. Sanitize since labels can contain dots/slashes."""
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", topic_label)
    return CACHE_DIR / f"{safe}.json"


def load_cached_candidates(topic_label: str) -> list[Paper] | None:
    """Load a cached candidate pool if it's fresh enough. Returns None if no
    cache exists or the cache is older than CACHE_MAX_AGE_DAYS.
    """
    path = _cache_path(topic_label)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        age = datetime.now(timezone.utc) - fetched_at
        if age.days >= CACHE_MAX_AGE_DAYS:
            print(f"  cache for {topic_label} is {age.days}d old; will refetch")
            return None
        papers = [Paper(**p) for p in data["papers"]]
        print(f"  cache hit for {topic_label}: {len(papers)} papers, {age.days}d old")
        return papers
    except Exception as e:
        print(f"  cache read failed for {topic_label}: {e!r}", file=sys.stderr)
        return None


def save_cached_candidates(topic_label: str, papers: list[Paper]) -> None:
    """Persist a freshly-fetched candidate pool for reuse this week."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(topic_label)
    data = {
        "topic": topic_label,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "papers": [p.__dict__ for p in papers],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  cached {len(papers)} papers for {topic_label}")


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


GEMINI_MAX_ATTEMPTS = 6      # was 5 — buys ~2 more minutes of runway for overload spikes
GEMINI_MAX_WAIT_5XX = 90     # cap per-attempt sleep so a bad run doesn't spiral
GEMINI_MAX_WAIT_429 = 300


def call_gemini(prompt: str, api_key: str, *, required: bool = True) -> str | None:
    """Send a single prompt to Gemini and return the text response.

    If `required` is False and every retry is exhausted, this returns None
    instead of raising, so callers with a sensible fallback (e.g. use the
    abstract instead of a Gemini-written brief) can keep the build going
    rather than aborting the whole run over one flaky API call.
    """
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
    for attempt in range(GEMINI_MAX_ATTEMPTS):
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
                wait = min(max(body_wait, 30 * (2 ** attempt)), GEMINI_MAX_WAIT_429)
            elif 500 <= e.code < 600:
                wait = min(5 * (2 ** attempt), GEMINI_MAX_WAIT_5XX)
            else:
                # 400/401/403 etc. won't fix themselves — fail fast.
                if required:
                    raise
                print(f"  Gemini attempt {attempt+1}/{GEMINI_MAX_ATTEMPTS}: "
                      f"HTTP {e.code} (non-retryable); giving up on this call",
                      file=sys.stderr)
                return None
            print(f"  Gemini attempt {attempt+1}/{GEMINI_MAX_ATTEMPTS}: HTTP {e.code}; sleeping {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
        except Exception as e:
            last_err = e
            wait = min(5 * (2 ** attempt), GEMINI_MAX_WAIT_5XX)
            print(f"  Gemini attempt {attempt+1}/{GEMINI_MAX_ATTEMPTS} failed ({e!r}); sleeping {wait}s",
                  file=sys.stderr)
            time.sleep(wait)

    if required:
        raise RuntimeError(f"Gemini call failed after retries: {last_err}")
    print(f"  Gemini call failed after {GEMINI_MAX_ATTEMPTS} attempts ({last_err!r}); "
          f"continuing without it", file=sys.stderr)
    return None


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
    # Build today_label manually because %-d (no zero-pad) is not portable to
    # Windows — strftime there raises ValueError. f-string with int() handles it.
    today_label = f"{now.strftime('%A, %B')} {now.day}, {now.year}"
    generated_at = now.strftime("%Y-%m-%d %H:%M UTC")

    years = years_back_for(topic_label)
    print(f"[{today_iso}] Topic: {topic_label} ({category_query}); window: last {years} years")

    # Try cache first — if a fresh pool exists for this topic, skip arXiv entirely.
    candidates = load_cached_candidates(topic_label)

    if not candidates:
        try:
            candidates = fetch_candidates(category_query, years_back=years)
            if candidates:
                save_cached_candidates(topic_label, candidates)
        except Exception as e:
            print(f"  fetch failed: {e!r}", file=sys.stderr)
            candidates = []

        # If fresh fetch failed, last-ditch: try a stale cache (any age)
        if not candidates:
            stale_path = _cache_path(topic_label)
            if stale_path.exists():
                try:
                    data = json.loads(stale_path.read_text(encoding="utf-8"))
                    candidates = [Paper(**p) for p in data["papers"]]
                    print(f"  using STALE cache for {topic_label}: {len(candidates)} papers")
                except Exception as e:
                    print(f"  stale cache read failed: {e!r}", file=sys.stderr)

    if not candidates:
        print("No candidates returned and no cache; aborting", file=sys.stderr)
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
        required=False,
    )
    if brief is None:
        print("  brief summary unavailable; falling back to the raw abstract",
              file=sys.stderr)
        brief = paper.abstract

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
        required=False,
    )
    if deep_raw is not None:
        deep_html = render_deep_summary_html(deep_raw)
    else:
        print("  deep summary unavailable; publishing brief-only today",
              file=sys.stderr)
        deep_html = (
            "<p><em>The deep explainer couldn't be generated today "
            "&mdash; the AI service was temporarily unavailable. "
            "Check back tomorrow, or read the abstract and full paper "
            "via the links above.</em></p>"
        )

    # Generate spoken-audio version of the brief. Non-fatal if it fails.
    print("Synthesizing audio...")
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    # Pick a non-colliding archive filename stem for today. If today's date file
    # already exists, append a counter: 2026-05-15.html, then 2026-05-15-2.html,
    # 2026-05-15-3.html, etc. This way running multiple times in one day keeps
    # all picks instead of overwriting earlier ones.
    archive_stem = today_iso
    if (ARCHIVE_DIR / f"{archive_stem}.html").exists():
        n = 2
        while (ARCHIVE_DIR / f"{today_iso}-{n}.html").exists():
            n += 1
        archive_stem = f"{today_iso}-{n}"
    print(f"Archive stem for this run: {archive_stem}")

    audio_path = SITE_DIR / "today.mp3"
    audio_ok = synthesize_brief_audio(brief, audio_path)
    if audio_ok:
        # Archive a per-stem copy so old entries keep their audio intact.
        archive_audio = ARCHIVE_DIR / f"{archive_stem}.mp3"
        archive_audio.write_bytes(audio_path.read_bytes())
        size_kb = audio_path.stat().st_size // 1024
        print(f"  wrote site/today.mp3 ({size_kb} KB) and archive/{archive_stem}.mp3")
    else:
        # Stale audio from a previous run would mismatch today's text.
        if audio_path.exists():
            audio_path.unlink()
        print("  audio synthesis failed; page will fall back to browser TTS")

    # Render index.html — references today.mp3 (latest audio)
    html_index = render_page(
        paper, brief, deep_html, generated_at, today_label,
        audio_url="today.mp3" if audio_ok else "",
    )
    (SITE_DIR / "index.html").write_text(html_index, encoding="utf-8")

    # Render the archived copy — references its own dated audio file so reruns
    # don't clobber earlier picks
    html_archive = render_page(
        paper, brief, deep_html, generated_at, today_label,
        audio_url=f"{archive_stem}.mp3" if audio_ok else "",
    )
    (ARCHIVE_DIR / f"{archive_stem}.html").write_text(html_archive, encoding="utf-8")

    # Ship the favorites page alongside the index. It's static and the same
    # every day; just copy it from the scripts/ directory.
    favs_src = ROOT / "scripts" / "favorites_page.html"
    if favs_src.exists():
        (SITE_DIR / "favorites.html").write_text(
            favs_src.read_text(encoding="utf-8"), encoding="utf-8"
        )

    print(f"Wrote site/index.html and archive/{archive_stem}.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
