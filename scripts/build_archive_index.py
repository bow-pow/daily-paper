"""
Builds site/archive/index.html — a chronological listing of past daily pages.
Run after build.py each day.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = ROOT / "site" / "archive"

TITLE_RE = re.compile(r'<h1 class="paper-title">(.*?)</h1>', re.S)
CATS_RE  = re.compile(r'<span>([^<]+)</span>\s*<span>Submitted', re.S)


def extract_title(html: str) -> str:
    m = TITLE_RE.search(html)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else "(untitled)"


def extract_cats(html: str) -> str:
    m = CATS_RE.search(html)
    return m.group(1).strip() if m else ""


def _sort_key(p):
    """Sort archive entries so the newest pick of the newest date comes first.
    Stem looks like '2026-05-15' or '2026-05-15-2' (suffix for same-day reruns).
    """
    stem = p.stem
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:-(\d+))?$", stem)
    if not m:
        return ("", 0)
    date_str = m.group(1)
    suffix = int(m.group(2)) if m.group(2) else 1
    return (date_str, suffix)


def main() -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        (p for p in ARCHIVE_DIR.glob("*.html") if p.name != "index.html"),
        key=_sort_key,
        reverse=True,
    )
    rows = []
    for f in files:
        date = f.stem
        html = f.read_text(encoding="utf-8", errors="ignore")
        title = extract_title(html)
        cats = extract_cats(html)
        rows.append(
            f'<li><a href="{f.name}"><span class="d">{date}</span>'
            f'<span class="t">{title}</span>'
            f'<span class="c">{cats}</span></a></li>'
        )

    page = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Archive — Daily Paper</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@400;600;700&family=Inter:wght@400;500&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
  :root {{ --paper:#f4efe6; --ink:#1f1d1a; --ink-faded:#7a7468; --accent:#8a3324; --rule:#c9c1b0; }}
  body {{ margin:0; background:var(--paper); color:var(--ink); font-family:'Fraunces',serif; }}
  .page {{ max-width:760px; margin:0 auto; padding:56px 28px 96px; }}
  header {{ border-bottom:1px solid var(--rule); padding-bottom:18px; margin-bottom:36px;
            display:flex; justify-content:space-between; align-items:baseline; }}
  .brand {{ font-weight:700; letter-spacing:.04em; text-transform:uppercase; }}
  .brand .dot {{ color:var(--accent); margin:0 .2em; }}
  h1 {{ font-weight:600; font-size:2.2rem; letter-spacing:-.015em; margin:0 0 28px; }}
  a.home {{ font-family:'Inter',sans-serif; font-size:.78rem; color:var(--ink-faded);
            text-decoration:none; letter-spacing:.08em; text-transform:uppercase; }}
  a.home:hover {{ color:var(--accent); }}
  ul {{ list-style:none; padding:0; margin:0; }}
  li {{ border-bottom:1px solid var(--rule); }}
  li a {{ display:grid; grid-template-columns:120px 1fr auto; gap:18px; align-items:baseline;
          padding:18px 4px; text-decoration:none; color:var(--ink); transition:background .15s; }}
  li a:hover {{ background:rgba(138,51,36,.05); }}
  .d {{ font-family:'JetBrains Mono',monospace; font-size:.78rem; color:var(--accent); letter-spacing:.04em; }}
  .t {{ font-size:1.05rem; line-height:1.4; }}
  .c {{ font-family:'Inter',sans-serif; font-size:.72rem; color:var(--ink-faded);
        text-transform:uppercase; letter-spacing:.08em; }}
  @media (max-width:520px) {{
    li a {{ grid-template-columns:1fr; gap:4px; }}
    .c {{ display:none; }}
  }}
</style></head><body>
<div class="page">
  <header>
    <div class="brand">Daily<span class="dot">·</span>Paper</div>
    <a class="home" href="../">← today</a>
  </header>
  <h1>Archive</h1>
  <ul>
    {''.join(rows) if rows else '<li style="padding:18px 4px;color:var(--ink-faded)">No entries yet.</li>'}
  </ul>
</div></body></html>"""

    (ARCHIVE_DIR / "index.html").write_text(page, encoding="utf-8")
    print(f"Archive index written with {len(rows)} entries.")


if __name__ == "__main__":
    main()
