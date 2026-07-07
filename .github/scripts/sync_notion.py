"""
sync_notion.py
Fetches Notion writeup pages, downloads images, and generates styled HTML files.
Run by GitHub Actions — requires NOTION_TOKEN secret.
"""

import os, re, sys, json, time, hashlib, requests
from pathlib import Path

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
}

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WRITEUPS_DIR = REPO_ROOT / "writeups"
IMAGES_DIR   = WRITEUPS_DIR / "images"

PAGES = [
    {"id": "394b7523-7172-809d-804f-c94eea64b1de", "slug": "pickle-rick",       "out": "pickle-rick.html"},
    {"id": "395b7523-7172-80f3-a2d5-c67e92cd0af5", "slug": "kioptrix-level-1",  "out": "kioptrix-level-1.html"},
    {"id": "396b7523-7172-804f-a8ac-ef1531530fda", "slug": "kioptrix-level-2",  "out": "kioptrix-level-2.html"},
]

# ── Notion API helpers ──────────────────────────────────────────────────────

def notion_get(path, **params):
    url = f"https://api.notion.com/v1{path}"
    while True:
        r = requests.get(url, headers=HEADERS, params=params)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 2)))
            continue
        r.raise_for_status()
        return r.json()

def get_page(page_id):
    return notion_get(f"/pages/{page_id}")

def get_blocks(block_id):
    blocks, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = notion_get(f"/blocks/{block_id}/children", **params)
        blocks.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    # fetch children recursively
    for b in blocks:
        if b.get("has_children"):
            b["_children"] = get_blocks(b["id"])
    return blocks

# ── Image download ──────────────────────────────────────────────────────────

def download_image(url, slug, block_id):
    """Download image to repo and return relative path. Returns None on failure."""
    img_dir = IMAGES_DIR / slug
    img_dir.mkdir(parents=True, exist_ok=True)
    ext = "png"
    m = re.search(r"\.(?:png|jpg|jpeg|gif|webp|svg)(?=[?&#]|$)", url, re.I)
    if m:
        ext = m.group().lstrip(".").lower()
    # Use full block id (no dashes) for unique filenames
    fname = f"{block_id.replace('-', '')}.{ext}"
    dest  = img_dir / fname
    if not dest.exists():
        print(f"  Downloading {fname} …")
        try:
            r = requests.get(url, timeout=60, allow_redirects=True)
            r.raise_for_status()
            # Validate it's actually image data
            ct = r.headers.get("content-type", "")
            if "text/html" in ct:
                print(f"  WARNING: got HTML instead of image (URL may be expired): {fname}")
                return None
            dest.write_bytes(r.content)
            print(f"  Saved {len(r.content):,} bytes → {fname}")
        except Exception as e:
            print(f"  WARNING: download failed for {fname}: {e}")
            return None
    return f"images/{slug}/{fname}"

# ── Rich-text renderer ──────────────────────────────────────────────────────

def rt(rich_texts):
    out = ""
    for t in rich_texts:
        text  = t.get("plain_text", "")
        ann   = t.get("annotations", {})
        href  = (t.get("href") or "")
        text  = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if ann.get("code"):        text = f"<code>{text}</code>"
        if ann.get("bold"):        text = f"<strong>{text}</strong>"
        if ann.get("italic"):      text = f"<em>{text}</em>"
        if ann.get("strikethrough"): text = f"<s>{text}</s>"
        if ann.get("underline"):   text = f"<u>{text}</u>"
        color = ann.get("color", "default")
        if color and color != "default" and not color.endswith("_background"):
            text = f'<span style="color:var(--c-{color})">{text}</span>'
        if href:
            text = f'<a href="{href}" target="_blank" rel="noopener">{text}</a>'
        out += text
    return out

# ── Block renderer ──────────────────────────────────────────────────────────

def render_blocks(blocks, slug, depth=0):
    html   = ""
    i      = 0
    indent = "  " * depth

    while i < len(blocks):
        b    = blocks[i]
        btype = b["type"]
        data  = b.get(btype, {})
        bid   = b["id"]

        # ── Lists: group consecutive items ──
        if btype == "bulleted_list_item":
            html += f"{indent}<ul>\n"
            while i < len(blocks) and blocks[i]["type"] == "bulleted_list_item":
                bd = blocks[i][blocks[i]["type"]]
                children_html = ""
                if blocks[i].get("_children"):
                    children_html = render_blocks(blocks[i]["_children"], slug, depth+1)
                html += f"{indent}  <li>{rt(bd.get('rich_text', []))}{children_html}</li>\n"
                i += 1
            html += f"{indent}</ul>\n"
            continue

        if btype == "numbered_list_item":
            html += f"{indent}<ol>\n"
            while i < len(blocks) and blocks[i]["type"] == "numbered_list_item":
                bd = blocks[i][blocks[i]["type"]]
                children_html = ""
                if blocks[i].get("_children"):
                    children_html = render_blocks(blocks[i]["_children"], slug, depth+1)
                html += f"{indent}  <li>{rt(bd.get('rich_text', []))}{children_html}</li>\n"
                i += 1
            html += f"{indent}</ol>\n"
            continue

        # ── Individual block types ──
        if btype == "paragraph":
            text = rt(data.get("rich_text", []))
            html += f"{indent}<p>{text}</p>\n" if text else f"{indent}<br>\n"

        elif btype in ("heading_1", "heading_2", "heading_3"):
            n    = btype[-1]
            text = rt(data.get("rich_text", []))
            slug_id = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
            html += f'{indent}<h{n} id="{slug_id}">{text}</h{n}>\n'

        elif btype == "divider":
            html += f"{indent}<hr>\n"

        elif btype == "quote":
            text = rt(data.get("rich_text", []))
            html += f"{indent}<blockquote>{text}</blockquote>\n"

        elif btype == "code":
            lang = data.get("language", "")
            text = rt(data.get("rich_text", []))
            html += f'{indent}<pre><code class="language-{lang}">{text}</code></pre>\n'

        elif btype == "callout":
            icon = (data.get("icon") or {})
            emoji = icon.get("emoji", "ℹ️") if icon.get("type") == "emoji" else "ℹ️"
            text  = rt(data.get("rich_text", []))
            html += f'{indent}<div class="callout"><span class="callout-icon">{emoji}</span><div>{text}</div></div>\n'

        elif btype == "image":
            src_type = data.get("type", "external")
            url = data.get(src_type, {}).get("url", "")
            caption = rt(data.get("caption", []))
            if url:
                local = download_image(url, slug, bid)
                img_src = local if local else url  # fall back to S3 URL if download failed
                html += f'{indent}<figure><img src="{img_src}" alt="{caption or "screenshot"}" loading="lazy">'
                if caption:
                    html += f"<figcaption>{caption}</figcaption>"
                html += "</figure>\n"

        elif btype == "table":
            has_header = data.get("has_column_header", False)
            children   = b.get("_children", [])
            html += f"{indent}<div class='table-wrap'><table>\n"
            for ri, row in enumerate(children):
                cells = row.get("table_row", {}).get("cells", [])
                tag   = "th" if (has_header and ri == 0) else "td"
                html += f"{indent}  <tr>"
                for cell in cells:
                    html += f"<{tag}>{rt(cell)}</{tag}>"
                html += "</tr>\n"
            html += f"{indent}</table></div>\n"

        elif btype == "toggle":
            summary = rt(data.get("rich_text", []))
            inner   = render_blocks(b.get("_children", []), slug, depth+1) if b.get("_children") else ""
            html += f"{indent}<details><summary>{summary}</summary>\n{inner}{indent}</details>\n"

        elif btype == "column_list":
            children = b.get("_children", [])
            html += f'{indent}<div class="columns">\n'
            for col in children:
                col_inner = render_blocks(col.get("_children", []), slug, depth+2)
                html += f'{indent}  <div class="column">\n{col_inner}{indent}  </div>\n'
            html += f"{indent}</div>\n"

        else:
            # Fallback: render any rich_text if present
            text = rt(data.get("rich_text", []))
            if text:
                html += f"{indent}<p>{text}</p>\n"

        i += 1

    return html


# ── HTML page template ──────────────────────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} — Waseef's Portfolio</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
      --border: #30363d; --text: #e6edf3; --muted: #8b949e;
      --accent: #58a6ff; --accent2: #3fb950; --red: #f85149;
      --orange: #d29922; --radius: 8px;
      --c-red: #f85149; --c-blue: #58a6ff; --c-green: #3fb950;
      --c-yellow: #d29922; --c-purple: #bc8cff; --c-pink: #ff7b72;
      --c-gray: #8b949e;
    }}
    html {{ scroll-behavior: smooth; }}
    body {{ background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; font-size: 15px; line-height: 1.7; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    /* Nav */
    nav {{ position: sticky; top: 0; z-index: 100; background: rgba(13,17,23,.9); backdrop-filter: blur(12px);
           border-bottom: 1px solid var(--border); padding: .75rem 2rem; display: flex; align-items: center; justify-content: space-between; }}
    .nav-logo {{ font-weight: 600; font-size: 1rem; color: var(--text); }}
    .nav-back {{ font-size: .85rem; color: var(--muted); display: flex; align-items: center; gap: .4rem; }}
    .nav-back:hover {{ color: var(--accent); text-decoration: none; }}

    /* Hero */
    .hero {{ background: var(--bg2); border-bottom: 1px solid var(--border); padding: 3rem 2rem 2.5rem; }}
    .hero-inner {{ max-width: 860px; margin: 0 auto; }}
    .breadcrumb {{ font-size: .8rem; color: var(--muted); margin-bottom: 1rem; }}
    .breadcrumb a {{ color: var(--muted); }}
    .breadcrumb a:hover {{ color: var(--accent); }}
    .hero h1 {{ font-size: 2rem; font-weight: 700; margin-bottom: 1rem; }}
    .meta-row {{ display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; }}
    .badge {{ display: inline-flex; align-items: center; gap: .3rem; padding: .25rem .7rem;
              border-radius: 20px; font-size: .75rem; font-weight: 500; }}
    .badge-platform {{ background: #1f3a5f; color: #58a6ff; border: 1px solid #1f4080; }}
    .badge-easy {{ background: #1a3a2a; color: #3fb950; border: 1px solid #2ea043; }}
    .badge-medium {{ background: #3a2a1a; color: #d29922; border: 1px solid #9e6a03; }}
    .badge-hard {{ background: #3a1a1a; color: #f85149; border: 1px solid #da3633; }}
    .badge-beginner {{ background: #1a3a2a; color: #56d364; border: 1px solid #2ea043; }}
    .badge-os {{ background: var(--bg3); color: var(--muted); border: 1px solid var(--border); }}
    .badge-date {{ background: var(--bg3); color: var(--muted); border: 1px solid var(--border); }}

    /* Content */
    .content {{ max-width: 860px; margin: 0 auto; padding: 3rem 2rem 5rem; }}
    h1, h2, h3 {{ color: var(--text); font-weight: 600; margin: 2rem 0 .75rem; }}
    h2 {{ font-size: 1.35rem; padding-bottom: .5rem; border-bottom: 1px solid var(--border); }}
    h3 {{ font-size: 1.1rem; color: var(--accent); }}
    p {{ margin: .6rem 0; color: #c9d1d9; }}
    ul, ol {{ margin: .5rem 0 .5rem 1.5rem; }}
    li {{ margin: .3rem 0; color: #c9d1d9; }}
    hr {{ border: none; border-top: 1px solid var(--border); margin: 1.5rem 0; }}
    blockquote {{ border-left: 3px solid var(--accent); padding: .5rem 1rem; margin: .75rem 0;
                  background: var(--bg2); border-radius: 0 var(--radius) var(--radius) 0; color: var(--muted); }}
    pre {{ background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius);
           padding: 1rem 1.25rem; overflow-x: auto; margin: .75rem 0; }}
    code {{ font-family: 'Fira Code', monospace; font-size: .85rem; color: #e6edf3; }}
    p code {{ background: var(--bg3); padding: .1rem .35rem; border-radius: 4px; color: var(--accent); font-size: .82rem; }}
    strong code {{ background: var(--bg3); padding: .1rem .35rem; border-radius: 4px; }}
    figure {{ margin: 1.25rem 0; }}
    img {{ max-width: 100%; border-radius: var(--radius); border: 1px solid var(--border);
           display: block; margin: 0 auto; }}
    figcaption {{ text-align: center; font-size: .8rem; color: var(--muted); margin-top: .4rem; }}
    .callout {{ display: flex; gap: .75rem; align-items: flex-start; background: var(--bg2);
                border: 1px solid var(--border); border-radius: var(--radius); padding: .9rem 1rem; margin: .75rem 0; }}
    .callout-icon {{ font-size: 1.1rem; flex-shrink: 0; margin-top: .1rem; }}
    details {{ border: 1px solid var(--border); border-radius: var(--radius); margin: .5rem 0; }}
    summary {{ padding: .6rem 1rem; cursor: pointer; font-weight: 500; background: var(--bg2); border-radius: var(--radius); }}
    details[open] summary {{ border-radius: var(--radius) var(--radius) 0 0; border-bottom: 1px solid var(--border); }}
    details > *:not(summary) {{ padding: .75rem 1rem; }}
    .table-wrap {{ overflow-x: auto; margin: .75rem 0; }}
    table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
    th, td {{ padding: .6rem .9rem; border: 1px solid var(--border); text-align: left; }}
    th {{ background: var(--bg3); font-weight: 600; }}
    tr:nth-child(even) {{ background: rgba(33,38,45,.5); }}
    .columns {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1rem; margin: .75rem 0; }}

    /* Footer */
    footer {{ border-top: 1px solid var(--border); padding: 2rem; text-align: center; color: var(--muted); font-size: .85rem; }}
  </style>
</head>
<body>
<nav>
  <span class="nav-logo">Waseef</span>
  <a href="../index.html" class="nav-back">← Back to Portfolio</a>
</nav>

<div class="hero">
  <div class="hero-inner">
    <div class="breadcrumb"><a href="../index.html">Portfolio</a> / <a href="../index.html#labs">Labs</a> / {title}</div>
    <h1>{title}</h1>
    <div class="meta-row">
      {badges}
    </div>
  </div>
</div>

<div class="content">
{body}
</div>

<footer>Built by Waseef · <a href="../index.html">Back to Portfolio</a></footer>
</body>
</html>
"""


def build_badges(page):
    props = page.get("properties", {})
    badges = ""

    def text_prop(key):
        p = props.get(key, {})
        if p.get("type") == "title":
            return "".join(r["plain_text"] for r in p.get("title", []))
        if p.get("type") == "rich_text":
            return "".join(r["plain_text"] for r in p.get("rich_text", []))
        if p.get("type") == "select":
            s = p.get("select")
            return s["name"] if s else ""
        if p.get("type") == "date":
            d = p.get("date")
            return d["start"][:10] if d else ""
        return ""

    title   = text_prop("title") or text_prop("Name")
    # We'll detect platform/difficulty/OS from page content if not in properties
    return title


# ── Main ────────────────────────────────────────────────────────────────────

def extract_meta_from_blocks(blocks):
    """Pull Platform / OS / Difficulty / Date from the first table block."""
    meta = {}
    for b in blocks:
        if b["type"] == "table":
            for row in b.get("_children", []):
                cells = row.get("table_row", {}).get("cells", [])
                if len(cells) >= 2:
                    key = "".join(r["plain_text"] for r in cells[0]).strip().lower().replace("*", "")
                    val = "".join(r["plain_text"] for r in cells[1]).strip()
                    meta[key] = val
            break
    return meta


def make_badges(meta):
    html = ""
    if meta.get("platform"):
        html += f'<span class="badge badge-platform">🖥 {meta["platform"]}</span>\n      '
    diff = (meta.get("difficulty") or "").lower()
    if diff:
        cls  = f"badge-{diff}" if diff in ("easy","medium","hard","beginner") else "badge-os"
        icon = {"easy":"🟢","medium":"🟡","hard":"🔴","beginner":"🟢"}.get(diff, "⚪")
        html += f'<span class="badge {cls}">{icon} {meta.get("difficulty")}</span>\n      '
    if meta.get("os"):
        html += f'<span class="badge badge-os">🐧 {meta["os"]}</span>\n      '
    if meta.get("date"):
        html += f'<span class="badge badge-date">📅 {meta["date"]}</span>\n      '
    return html.strip()


def sync_page(cfg):
    slug     = cfg["slug"]
    page_id  = cfg["id"]
    out_file = WRITEUPS_DIR / cfg["out"]

    print(f"\n{'='*50}")
    print(f"Syncing: {slug}")

    page   = get_page(page_id)
    title  = ""
    for _, pval in page.get("properties", {}).items():
        if pval.get("type") == "title":
            title = "".join(r["plain_text"] for r in pval.get("title", []))
            break

    print(f"  Title: {title}")
    blocks = get_blocks(page_id)
    meta   = extract_meta_from_blocks(blocks)
    badges = make_badges(meta)
    body   = render_blocks(blocks, slug)

    html = HTML_TEMPLATE.format(title=title, badges=badges, body=body)
    out_file.write_text(html, encoding="utf-8")
    print(f"  Written → {out_file.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    WRITEUPS_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    for cfg in PAGES:
        try:
            sync_page(cfg)
        except Exception as e:
            print(f"ERROR on {cfg['slug']}: {e}", file=sys.stderr)
            sys.exit(1)
    print("\n✅ All writeups synced.")
