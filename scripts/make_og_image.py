"""
Generates the Open Graph preview banner.
Run once locally; the resulting og-image.png is committed and served from /site.
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import urllib.request

# LinkedIn / Twitter recommended size
W, H = 1200, 630

# Site palette
PAPER = (244, 239, 230)
INK = (31, 29, 26)
INK_SOFT = (74, 70, 63)
INK_FADED = (122, 116, 104)
ACCENT = (138, 51, 36)
RULE = (201, 193, 176)


def load_font(url, name, size):
    """Try to load Fraunces from cache or download. Fall back to a system serif
    if the download fails (e.g. sandboxed environment, offline)."""
    fonts_dir = Path(__file__).parent / "_fonts"
    fonts_dir.mkdir(exist_ok=True)
    path = fonts_dir / name
    if not path.exists():
        try:
            urllib.request.urlretrieve(url, path)
        except Exception as e:
            print(f"  font download failed ({e}); using system fallback", flush=True)
            # Fallback to whatever serif the system has
            for candidate in [
                "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
                "C:\\Windows\\Fonts\\georgiab.ttf",
                "C:\\Windows\\Fonts\\georgia.ttf",
                "/System/Library/Fonts/Georgia.ttf",
            ]:
                if Path(candidate).exists():
                    return ImageFont.truetype(candidate, size)
            return ImageFont.load_default()
    return ImageFont.truetype(str(path), size)


# Use Google's CDN copies — same fonts the site loads
FRAUNCES_BOLD = "https://github.com/undercasetype/Fraunces/raw/main/fonts/static/Fraunces/Fraunces-Bold.ttf"
FRAUNCES_REG  = "https://github.com/undercasetype/Fraunces/raw/main/fonts/static/Fraunces/Fraunces-Regular.ttf"
INTER_MED     = "https://github.com/rsms/inter/raw/master/docs/font-files/Inter-Medium.woff2"  # fallback to system
# Inter as TTF
INTER_REG_TTF = "https://github.com/rsms/inter/raw/master/docs/font-files/Inter-Regular.otf"
INTER_ITAL    = "https://github.com/rsms/inter/raw/master/docs/font-files/Inter-Italic.otf"


def make_banner():
    img = Image.new("RGB", (W, H), PAPER)
    draw = ImageDraw.Draw(img)

    # Subtle accent radial in top-left (matches site's background gradient feel)
    for r in range(380, 0, -8):
        alpha = int(8 * (1 - r / 380))
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        odraw.ellipse([120 - r, 80 - r, 120 + r, 80 + r],
                      fill=(*ACCENT, alpha))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Fonts
    f_brand   = load_font(FRAUNCES_BOLD, "Fraunces-Bold.ttf", 64)
    f_byline  = load_font(FRAUNCES_REG,  "Fraunces-Regular.ttf", 30)
    f_tag     = load_font(FRAUNCES_REG,  "Fraunces-Regular.ttf", 38)
    f_small   = load_font(FRAUNCES_REG,  "Fraunces-Regular.ttf", 24)

    # Top thin rule
    draw.rectangle([(80, 96), (W - 80, 97)], fill=RULE)

    # Top-bar metadata
    draw.text((80, 50), "ONE  JOURNAL  ·  A  DAY", fill=INK, font=f_small)
    # Right-align date placeholder
    right_text = "DAILY · arXiv + AI"
    rt_w = draw.textlength(right_text, font=f_small)
    draw.text((W - 80 - rt_w, 50), right_text, fill=INK_FADED, font=f_small)

    # Big title centerpiece — multi-line
    title_lines = ["One paper.", "Explained plainly.", "Every morning."]
    y = 200
    for line in title_lines:
        # Highlight middle line with subtle accent for visual punch
        color = ACCENT if line == "Explained plainly." else INK
        draw.text((80, y), line, fill=color, font=f_brand)
        y += 78

    # Tagline below
    tag = "Physics · Astrophysics · Math · AI · Quantum"
    draw.text((80, y + 16), tag, fill=INK_SOFT, font=f_tag)

    # Bottom byline + arXiv mention
    draw.rectangle([(80, H - 80), (W - 80, H - 79)], fill=RULE)
    draw.text((80, H - 60), "by Abhijeet Sharma", fill=INK, font=f_byline)
    src = "Powered by arXiv, Gemini, edge-tts"
    src_w = draw.textlength(src, font=f_small)
    draw.text((W - 80 - src_w, H - 54), src, fill=INK_FADED, font=f_small)

    out = Path(__file__).parent.parent / "site" / "og-banner.png"
    img.save(out, "PNG", optimize=True)
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB, {W}x{H})")


if __name__ == "__main__":
    make_banner()
