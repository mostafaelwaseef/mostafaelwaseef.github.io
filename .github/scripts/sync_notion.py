"""
sync_notion.py
Fully auto-discovering Notion -> static site sync.

No hardcoded page IDs. On every run this script:
  1. Searches the whole Notion workspace the integration can see.
  2. Treats any top-level page whose content has a heading containing the
     word "Index" (e.g. "Machines Index", "Labs Index", "Levels Index") as
     a *root* page (mirrors the "main page + sub-pages" pattern used in
     Notion for Machines / PortSwigger Labs / OTW Wargames).
  3. Renders every child (sub-)page of that root into standalone HTML,
     with all images downloaded locally -- nothing links back to Notion.
  4. Regenerates the Machines / Labs / Wargames sections of index.html.
  5. Deletes any generated files that no longer correspond to something
     in Notion (renamed/removed pages don't leave orphans behind).

Run by GitHub Actions -- requires the NOTION_TOKEN secret.
"""

import os, re, sys, time, requests
from pathlib import Path

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
}

REPO_ROOT    = Path(__file__).resolve().parent.parent.parent
WRITEUPS_DIR = REPO_ROOT / "writeups"
INDEX_HTML   = REPO_ROOT / "index.html"

# Every file this run creates or keeps -- anything else under writeups/ gets pruned.
EXPECTED = set()

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

def notion_post(path, body):
    url = f"https://api.notion.com/v1{path}"
    while True:
        r = requests.post(url, headers=HEADERS, json=body)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 2)))
            continue
        r.raise_for_status()
        return r.json()

def get_page(page_id):
    return notion_get(f"/pages/{page_id}")

def get_block_children(block_id):
    """Direct children only (one level), fully paginated."""
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
    return blocks

def get_blocks(block_id):
    """Recursive fetch for rendering a leaf page's full content.
    Never recurses INTO a child_page/child_database -- those are separate
    pages, not content of this one."""
    blocks = get_block_children(block_id)
    for b in blocks:
        if b.get("has_children") and b["type"] not in ("child_page", "child_database"):
            b["_children"] = get_blocks(b["id"])
    return blocks

def search_all_pages():
    """All Notion pages the integration has access to."""
    pages, cursor = [], None
    while True:
        body = {"filter": {"property": "object", "value": "page"}, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_post("/search", body)
        pages.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return pages

def get_title(page_obj):
    for pval in page_obj.get("properties", {}).values():
        if pval.get("type") == "title":
            return "".join(r["plain_text"] for r in pval.get("title", []))
    return ""

# ── Text / slug helpers ─────────────────────────────────────────────────────

def plain(rich_texts):
    return "".join(t.get("plain_text", "") for t in rich_texts)

def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def split_title(t):
    """Split on ' — ' (em dash), the convention used for 'Name — Platform'."""
    return [p.strip() for p in re.split(r"\s+—\s+", t) if p.strip()]

def clean_segment(s):
    """Strip emoji / non-ascii symbols, collapse whitespace."""
    s = re.sub(r"[^\x00-\x7f]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def slugify(s):
    s = clean_segment(s).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")

def extract_icon(raw_title):
    m = re.match(r"^([^\x00-\x7f]+)\s*", raw_title)
    return m.group(1).strip() if m else "🗂️"

def find_index_heading(flat_blocks):
    for b in flat_blocks:
        if b["type"] in ("heading_1", "heading_2", "heading_3"):
            text = plain(b[b["type"]].get("rich_text", []))
            if "index" in text.lower():
                return text
    return None

def classify_kind(heading_text):
    low = heading_text.lower()
    if "machine" in low:
        return "machines"
    if "level" in low:
        return "wargames"
    return "labs"

def get_excerpt(blocks, limit=160):
    for b in blocks:
        if b["type"] == "paragraph":
            text = plain(b["paragraph"].get("rich_text", [])).strip()
            if text:
                return text[:limit].rsplit(" ", 1)[0] + "…" if len(text) > limit else text
    return ""

# ── Image download ──────────────────────────────────────────────────────────

def download_image(url, img_dir, img_rel, block_id):
    img_dir.mkdir(parents=True, exist_ok=True)
    ext = "png"
    m = re.search(r"\.(?:png|jpg|jpeg|gif|webp|svg)(?=[?&#]|$)", url, re.I)
    if m:
        ext = m.group().lstrip(".").lower()
    fname = f"{block_id.replace('-', '')}.{ext}"
    dest  = img_dir / fname
    if not dest.exists():
        print(f"    Downloading {fname} …")
        try:
            r = requests.get(url, timeout=60, allow_redirects=True)
            r.raise_for_status()
            if "text/html" in r.headers.get("content-type", ""):
                print(f"    WARNING: got HTML instead of image (expired URL): {fname}")
                return None
            dest.write_bytes(r.content)
        except Exception as e:
            print(f"    WARNING: download failed for {fname}: {e}")
            return None
    EXPECTED.add(dest.resolve())
    return f"{img_rel}/{fname}"

# ── Rich-text renderer ──────────────────────────────────────────────────────

def rt(rich_texts):
    out = ""
    for t in rich_texts:
        text = t.get("plain_text", "")
        ann  = t.get("annotations", {})
        href = (t.get("href") or "")
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace("\n", "<br>")
        if ann.get("code"):          text = f"<code>{text}</code>"
        if ann.get("bold"):          text = f"<strong>{text}</strong>"
        if ann.get("italic"):        text = f"<em>{text}</em>"
        if ann.get("strikethrough"): text = f"<s>{text}</s>"
        if ann.get("underline"):     text = f"<u>{text}</u>"
        color = ann.get("color", "default")
        if color and color != "default" and not color.endswith("_background"):
            text = f'<span style="color:var(--c-{color})">{text}</span>'
        if href:
            text = f'<a href="{href}" target="_blank" rel="noopener">{text}</a>'
        out += text
    return out

# ── Block renderer ──────────────────────────────────────────────────────────

def render_blocks(blocks, ctx, depth=0):
    html   = ""
    i      = 0
    indent = "  " * depth

    while i < len(blocks):
        b     = blocks[i]
        btype = b["type"]
        data  = b.get(btype, {})
        bid   = b["id"]

        if btype == "bulleted_list_item":
            html += f"{indent}<ul>\n"
            while i < len(blocks) and blocks[i]["type"] == "bulleted_list_item":
                bd = blocks[i][blocks[i]["type"]]
                children_html = render_blocks(blocks[i]["_children"], ctx, depth + 1) if blocks[i].get("_children") else ""
                html += f"{indent}  <li>{rt(bd.get('rich_text', []))}{children_html}</li>\n"
                i += 1
            html += f"{indent}</ul>\n"
            continue

        if btype == "numbered_list_item":
            html += f"{indent}<ol>\n"
            while i < len(blocks) and blocks[i]["type"] == "numbered_list_item":
                bd = blocks[i][blocks[i]["type"]]
                children_html = render_blocks(blocks[i]["_children"], ctx, depth + 1) if blocks[i].get("_children") else ""
                html += f"{indent}  <li>{rt(bd.get('rich_text', []))}{children_html}</li>\n"
                i += 1
            html += f"{indent}</ol>\n"
            continue

        if btype == "paragraph":
            text = rt(data.get("rich_text", []))
            html += f"{indent}<p>{text}</p>\n" if text else f"{indent}<br>\n"

        elif btype in ("heading_1", "heading_2", "heading_3"):
            n = btype[-1]
            text = rt(data.get("rich_text", []))
            slug_id = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
            html += f'{indent}<h{n} id="{slug_id}">{text}</h{n}>\n'

        elif btype == "divider":
            html += f"{indent}<hr>\n"

        elif btype == "quote":
            text = rt(data.get("rich_text", []))
            children_html = render_blocks(b.get("_children", []), ctx, depth + 1) if b.get("_children") else ""
            html += f"{indent}<blockquote>{text}{children_html}</blockquote>\n"

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
                local = download_image(url, ctx["img_dir"], ctx["img_rel"], bid)
                img_src = local if local else url
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
            inner   = render_blocks(b.get("_children", []), ctx, depth + 1) if b.get("_children") else ""
            html += f"{indent}<details><summary>{summary}</summary>\n{inner}{indent}</details>\n"

        elif btype == "column_list":
            children = b.get("_children", [])
            html += f'{indent}<div class="columns">\n'
            for col in children:
                col_inner = render_blocks(col.get("_children", []), ctx, depth + 2)
                html += f'{indent}  <div class="column">\n{col_inner}{indent}  </div>\n'
            html += f"{indent}</div>\n"

        elif btype in ("child_page", "child_database"):
            pass  # sub-pages are synced as their own separate pages, not inlined

        else:
            text = rt(data.get("rich_text", []))
            if text:
                html += f"{indent}<p>{text}</p>\n"

        i += 1

    return html

# ── Meta / badges ────────────────────────────────────────────────────────────

def extract_meta_from_blocks(blocks):
    """Pull Platform / OS / Difficulty / Severity / Date etc. from the first table block."""
    meta = {}
    for b in blocks:
        if b["type"] == "table":
            for row in b.get("_children", []):
                cells = row.get("table_row", {}).get("cells", [])
                if len(cells) >= 2:
                    key = plain(cells[0]).strip().lower().replace("*", "")
                    val = plain(cells[1]).strip()
                    meta[key] = val
            break
    return meta

LEVEL_CLASS = {
    "easy": "badge-easy", "beginner": "badge-beginner", "low": "badge-easy",
    "medium": "badge-medium", "hard": "badge-hard", "high": "badge-hard",
    "critical": "badge-hard", "info": "badge-beginner",
}
LEVEL_ICON = {
    "easy": "🟢", "beginner": "🟢", "low": "🟢", "medium": "🟡",
    "hard": "🔴", "high": "🔴", "critical": "🔴", "info": "⚪",
}

def level_value(meta):
    return (meta.get("difficulty") or meta.get("severity") or "").strip()

def make_badges(meta):
    html = ""
    if meta.get("platform"):
        html += f'<span class="badge badge-platform">🖥 {esc(meta["platform"])}</span>\n      '
    level = level_value(meta)
    if level:
        low = level.lower()
        cls = LEVEL_CLASS.get(low, "badge-os")
        icon = LEVEL_ICON.get(low, "⚪")
        html += f'<span class="badge {cls}">{icon} {esc(level)}</span>\n      '
    if meta.get("os"):
        html += f'<span class="badge badge-os">🐧 {esc(meta["os"])}</span>\n      '
    if meta.get("date"):
        html += f'<span class="badge badge-date">📅 {esc(meta["date"])}</span>\n      '
    technique = meta.get("cipher / technique") or meta.get("technique")
    if technique:
        html += f'<span class="badge badge-os">🧩 {esc(technique)}</span>\n      '
    return html.strip()

def card_badge(meta):
    level = level_value(meta)
    if not level:
        return ""
    cls = LEVEL_CLASS.get(level.lower(), "badge-os")
    return f'<span class="badge {cls}">{esc(level)}</span>'

# ── Shared CSS + templates (plain string, no .format -- avoids brace escaping) ──

STYLE_BLOCK = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
      --border: #30363d; --text: #e6edf3; --muted: #8b949e;
      --accent: #58a6ff; --accent2: #3fb950; --red: #f85149;
      --orange: #d29922; --radius: 8px;
      --c-red: #f85149; --c-blue: #58a6ff; --c-green: #3fb950;
      --c-yellow: #d29922; --c-purple: #bc8cff; --c-pink: #ff7b72;
      --c-gray: #8b949e;
    }
    html { scroll-behavior: smooth; }
    body { background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; font-size: 15px; line-height: 1.7; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    nav { position: sticky; top: 0; z-index: 100; background: rgba(13,17,23,.9); backdrop-filter: blur(12px);
          border-bottom: 1px solid var(--border); padding: .75rem 2rem; display: flex; align-items: center; justify-content: space-between; }
    .nav-logo { font-weight: 600; font-size: 1rem; color: var(--text); }
    .nav-back { font-size: .85rem; color: var(--muted); display: flex; align-items: center; gap: .4rem; }
    .nav-back:hover { color: var(--accent); text-decoration: none; }
    .hero { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 3rem 2rem 2.5rem; }
    .hero-inner { max-width: 860px; margin: 0 auto; }
    .breadcrumb { font-size: .8rem; color: var(--muted); margin-bottom: 1rem; }
    .breadcrumb a { color: var(--muted); }
    .breadcrumb a:hover { color: var(--accent); }
    .hero h1 { font-size: 2rem; font-weight: 700; margin-bottom: 1rem; }
    .hero p.desc { color: var(--muted); max-width: 640px; margin-top: .5rem; }
    .meta-row { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; }
    .badge { display: inline-flex; align-items: center; gap: .3rem; padding: .25rem .7rem;
             border-radius: 20px; font-size: .75rem; font-weight: 500; }
    .badge-platform { background: #1f3a5f; color: #58a6ff; border: 1px solid #1f4080; }
    .badge-easy { background: #1a3a2a; color: #3fb950; border: 1px solid #2ea043; }
    .badge-medium { background: #3a2a1a; color: #d29922; border: 1px solid #9e6a03; }
    .badge-hard { background: #3a1a1a; color: #f85149; border: 1px solid #da3633; }
    .badge-beginner { background: #1a3a2a; color: #56d364; border: 1px solid #2ea043; }
    .badge-os { background: var(--bg3); color: var(--muted); border: 1px solid var(--border); }
    .badge-date { background: var(--bg3); color: var(--muted); border: 1px solid var(--border); }
    .content { max-width: 860px; margin: 0 auto; padding: 3rem 2rem 5rem; }
    h1, h2, h3 { color: var(--text); font-weight: 600; margin: 2rem 0 .75rem; }
    h2 { font-size: 1.35rem; padding-bottom: .5rem; border-bottom: 1px solid var(--border); }
    h3 { font-size: 1.1rem; color: var(--accent); }
    p { margin: .6rem 0; color: #c9d1d9; }
    ul, ol { margin: .5rem 0 .5rem 1.5rem; }
    li { margin: .3rem 0; color: #c9d1d9; }
    hr { border: none; border-top: 1px solid var(--border); margin: 1.5rem 0; }
    blockquote { border-left: 3px solid var(--accent); padding: .5rem 1rem; margin: .75rem 0;
                 background: var(--bg2); border-radius: 0 var(--radius) var(--radius) 0; color: var(--muted); }
    pre { background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius);
          padding: 1rem 1.25rem; overflow-x: auto; margin: .75rem 0; }
    code { font-family: 'Fira Code', monospace; font-size: .85rem; color: #e6edf3; }
    p code { background: var(--bg3); padding: .1rem .35rem; border-radius: 4px; color: var(--accent); font-size: .82rem; }
    strong code { background: var(--bg3); padding: .1rem .35rem; border-radius: 4px; }
    figure { margin: 1.25rem 0; }
    img { max-width: 100%; border-radius: var(--radius); border: 1px solid var(--border);
          display: block; margin: 0 auto; }
    figcaption { text-align: center; font-size: .8rem; color: var(--muted); margin-top: .4rem; }
    .callout { display: flex; gap: .75rem; align-items: flex-start; background: var(--bg2);
               border: 1px solid var(--border); border-radius: var(--radius); padding: .9rem 1rem; margin: .75rem 0; }
    .callout-icon { font-size: 1.1rem; flex-shrink: 0; margin-top: .1rem; }
    details { border: 1px solid var(--border); border-radius: var(--radius); margin: .5rem 0; }
    summary { padding: .6rem 1rem; cursor: pointer; font-weight: 500; background: var(--bg2); border-radius: var(--radius); }
    details[open] summary { border-radius: var(--radius) var(--radius) 0 0; border-bottom: 1px solid var(--border); }
    details > *:not(summary) { padding: .75rem 1rem; }
    .table-wrap { overflow-x: auto; margin: .75rem 0; }
    table { border-collapse: collapse; width: 100%; font-size: .9rem; }
    th, td { padding: .6rem .9rem; border: 1px solid var(--border); text-align: left; }
    th { background: var(--bg3); font-weight: 600; }
    tr:nth-child(even) { background: rgba(33,38,45,.5); }
    .columns { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1rem; margin: .75rem 0; }
    .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 1rem; margin-top: 1.5rem; }
    .card { background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); padding: 1.25rem;
            transition: border-color .15s, transform .15s; display: block; }
    .card:hover { border-color: var(--accent); transform: translateY(-2px); text-decoration: none; }
    .card-header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: .6rem; gap: .5rem; }
    .card-title { font-size: 14px; font-weight: 600; color: var(--text); }
    .card-meta { font-size: 12px; color: var(--muted); margin-bottom: .4rem; }
    .card-desc { font-size: 13px; color: var(--muted); }
    footer { border-top: 1px solid var(--border); padding: 2rem; text-align: center; color: var(--muted); font-size: .85rem; }
    @media (max-width: 620px) {
      nav { padding: .65rem 1.25rem; }
      .hero { padding: 2rem 1.25rem 1.75rem; }
      .hero h1 { font-size: 1.5rem; }
      .content { padding: 2rem 1.25rem 3rem; }
      .cards { grid-template-columns: 1fr; }
    }
"""

LEAF_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>__TITLE__ — Mostafa Elwaseef's Portfolio</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet" />
  <style>__STYLE__</style>
</head>
<body>
<nav>
  <span class="nav-logo">Waseef</span>
  <a href="__ROOT__index.html" class="nav-back">← Back to Portfolio</a>
</nav>

<div class="hero">
  <div class="hero-inner">
    <div class="breadcrumb">__BREADCRUMB__</div>
    <h1>__TITLE__</h1>
    <div class="meta-row">
      __BADGES__
    </div>
  </div>
</div>

<div class="content">
__BODY__
</div>

<footer>Built by Waseef · <a href="__ROOT__index.html">Back to Portfolio</a></footer>
</body>
</html>
"""

CATEGORY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>__TITLE__ — Mostafa Elwaseef's Portfolio</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet" />
  <style>__STYLE__</style>
</head>
<body>
<nav>
  <span class="nav-logo">Waseef</span>
  <a href="__ROOT__index.html" class="nav-back">← Back to Portfolio</a>
</nav>

<div class="hero">
  <div class="hero-inner">
    <div class="breadcrumb">__BREADCRUMB__</div>
    <h1>__ICON__ __TITLE__</h1>
    <p class="desc">__DESCRIPTION__</p>
  </div>
</div>

<div class="content">
  <div class="cards">
__CARDS__
  </div>
</div>

<footer>Built by Waseef · <a href="__ROOT__index.html">Back to Portfolio</a></footer>
</body>
</html>
"""

def render_template(tpl, **kwargs):
    out = tpl.replace("__STYLE__", STYLE_BLOCK)
    for k, v in kwargs.items():
        out = out.replace(f"__{k.upper()}__", v)
    return out

def leaf_card_html(entry):
    badge = card_badge(entry["meta"])
    header = f'<div class="card-header"><span class="card-title">{esc(entry["display"])}</span>{badge}</div>'
    meta_line = f'<div class="card-meta">{esc(entry["meta"].get("platform",""))}</div>' if entry["meta"].get("platform") else ""
    desc = f'<p class="card-desc">{esc(entry["excerpt"])}</p>' if entry["excerpt"] else ""
    return f'    <a href="{entry["slug"]}.html" class="card">\n      {header}\n      {meta_line}\n      {desc}\n    </a>\n'

# ── Sync a single leaf (machine / lab / level) page ─────────────────────────

def sync_leaf(leaf_id, fallback_title, kind, cat_slug=None, cat_label=None):
    page = get_page(leaf_id)
    title = get_title(page) or fallback_title
    blocks = get_blocks(leaf_id)
    meta = extract_meta_from_blocks(blocks)
    badges = make_badges(meta)
    excerpt = get_excerpt(blocks)

    if kind == "machines":
        slug = slugify(split_title(title)[0]) or slugify(title)
        out_dir = WRITEUPS_DIR / "machines"
        depth = 2
        root_rel = "../" * depth
        breadcrumb = f'<a href="{root_rel}index.html">Portfolio</a> / <a href="{root_rel}index.html#machines">Machines</a> / {esc(title)}'
        href_from_root = f"writeups/machines/{slug}.html"
    else:
        slug = slugify(title)
        out_dir = WRITEUPS_DIR / kind / cat_slug
        depth = 3
        root_rel = "../" * depth
        breadcrumb = (f'<a href="{root_rel}index.html">Portfolio</a> / '
                      f'<a href="{root_rel}index.html#{kind}">{kind.title()}</a> / '
                      f'<a href="index.html">{esc(cat_label)}</a> / {esc(title)}')
        href_from_root = f"writeups/{kind}/{cat_slug}/{slug}.html"

    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir = out_dir / "images" / slug
    ctx = {"img_dir": img_dir, "img_rel": f"images/{slug}"}
    body = render_blocks(blocks, ctx)

    html = render_template(LEAF_TEMPLATE, title=esc(title), root=root_rel, breadcrumb=breadcrumb, badges=badges, body=body)
    out_file = out_dir / f"{slug}.html"
    out_file.write_text(html, encoding="utf-8")
    EXPECTED.add(out_file.resolve())

    display = split_title(title)[0] if kind == "machines" else title
    return {"title": title, "display": display, "slug": slug, "meta": meta, "excerpt": excerpt, "href": href_from_root}

def write_category_index(kind, cat_slug, label, icon, leaves):
    out_dir = WRITEUPS_DIR / kind / cat_slug
    depth = 3
    root_rel = "../" * depth
    breadcrumb = f'<a href="{root_rel}index.html">Portfolio</a> / <a href="{root_rel}index.html#{kind}">{kind.title()}</a> / {esc(label)}'
    noun = "labs" if kind == "labs" else "levels"
    description = f"All {esc(label)} {noun} completed, synced automatically from Notion."
    cards = "".join(leaf_card_html(e) for e in leaves)

    html = render_template(CATEGORY_TEMPLATE, title=esc(label), root=root_rel, breadcrumb=breadcrumb,
                            icon=icon, description=description, cards=cards)
    out_file = out_dir / "index.html"
    out_file.write_text(html, encoding="utf-8")
    EXPECTED.add(out_file.resolve())
    return f"writeups/{kind}/{cat_slug}/index.html"

# ── index.html regeneration ─────────────────────────────────────────────────

def replace_between(text, name, new_inner):
    pattern = re.compile(rf"(<!-- AUTO:{name}:START -->)(.*?)(<!-- AUTO:{name}:END -->)", re.DOTALL)
    if not pattern.search(text):
        print(f"  WARNING: markers AUTO:{name} not found in index.html -- skipping")
        return text
    return pattern.sub(lambda m: m.group(1) + "\n" + new_inner + "\n      " + m.group(3), text)

def machine_card_html(entry):
    badge = card_badge(entry["meta"])
    header = f'<div class="card-header"><span class="card-title">{esc(entry["display"])}</span>{badge}</div>'
    meta_line = f'<div class="card-meta">{esc(entry["meta"].get("platform",""))}</div>' if entry["meta"].get("platform") else ""
    desc = f'<p style="font-size:13px;color:var(--muted);">{esc(entry["excerpt"])}</p>' if entry["excerpt"] else ""
    return f'      <a href="{entry["href"]}" class="card" style="text-decoration:none;">\n        {header}\n        {meta_line}\n        {desc}\n      </a>\n'

def category_tile_html(cat_slug, label, icon, href, count, kind):
    base = "lab" if kind == "labs" else "level"
    noun = base if count == 1 else base + "s"
    return (f'      <a href="{href}" class="lab-card" style="text-decoration:none;color:inherit;">'
            f'<div class="lab-icon">{icon}</div>'
            f'<div class="lab-info"><div class="lab-name">{esc(label)}</div>'
            f'<div class="lab-count">{count} {noun} completed</div></div></a>\n')

# PortSwigger Web Security Academy's own topic order, split into their
# server-side / client-side groupings. Unknown categories (a new topic
# added later in Notion) fall back to the end of the server-side group
# so nothing is silently dropped from the page.
SERVER_SIDE_ORDER = [
    "sql-injection", "authentication", "path-traversal", "command-injection",
    "os-command-injection", "business-logic-vulnerabilities",
    "information-disclosure", "access-control", "file-upload-vulnerabilities",
    "race-conditions", "ssrf", "server-side-request-forgery",
    "xxe-injection", "xxe", "request-smuggling", "server-side-template-injection",
    "ssti", "insecure-deserialization", "graphql-api-vulnerabilities",
    "web-cache-poisoning", "http-host-header-attacks",
    "oauth-authentication", "jwt", "prototype-pollution",
]
CLIENT_SIDE_ORDER = [
    "xss", "cross-site-scripting", "csrf", "cross-site-request-forgery",
    "cors", "cross-origin-resource-sharing", "clickjacking",
    "dom-based-vulnerabilities", "websockets", "web-cache-deception",
]

def category_group_order(slug):
    if slug in SERVER_SIDE_ORDER:
        return ("server", SERVER_SIDE_ORDER.index(slug))
    if slug in CLIENT_SIDE_ORDER:
        return ("client", CLIENT_SIDE_ORDER.index(slug))
    return ("server", 999)

def render_labs_groups(labs_buckets):
    server, client = [], []
    for slug, b in labs_buckets.items():
        group, order = category_group_order(slug)
        (server if group == "server" else client).append((order, slug, b))
    server.sort(key=lambda x: x[0])
    client.sort(key=lambda x: x[0])

    def group_block(title, items):
        if not items:
            return ""
        tiles = "".join(category_tile_html(slug, b["label"], b["icon"], b["href"], len(b["leaves"]), "labs")
                         for _, slug, b in items)
        return (f'    <div class="labs-group">\n'
                f'      <h3 class="labs-group-title">{esc(title)}</h3>\n'
                f'      <div class="labs-grid">\n{tiles}      </div>\n'
                f'    </div>\n')

    html = group_block("Server-Side", server) + group_block("Client-Side", client)
    return html or "      <p style=\"color:var(--muted);\">No labs synced yet.</p>\n"

def update_index_html(machines, labs_buckets, wargames_buckets):
    text = INDEX_HTML.read_text(encoding="utf-8")

    machines_html = "".join(machine_card_html(e) for e in machines) or "      <p style=\"color:var(--muted);\">No machines synced yet.</p>\n"
    text = replace_between(text, "MACHINES", machines_html)

    labs_html = render_labs_groups(labs_buckets)
    text = replace_between(text, "LABS", labs_html)

    wargames_html = "".join(
        category_tile_html(slug, b["label"], b["icon"], b["href"], len(b["leaves"]), "wargames")
        for slug, b in sorted(wargames_buckets.items())
    ) or "      <p style=\"color:var(--muted);\">No wargames synced yet.</p>\n"
    text = replace_between(text, "WARGAMES", wargames_html)

    INDEX_HTML.write_text(text, encoding="utf-8")

# ── Cleanup ──────────────────────────────────────────────────────────────────

def prune_orphans():
    if not WRITEUPS_DIR.exists():
        return
    removed = 0
    for path in WRITEUPS_DIR.rglob("*"):
        if path.is_file() and path.resolve() not in EXPECTED:
            path.unlink()
            removed += 1
    # remove now-empty directories, deepest first
    for path in sorted((p for p in WRITEUPS_DIR.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            path.rmdir()
        except OSError:
            pass
    if removed:
        print(f"Pruned {removed} orphaned file(s).")

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("Searching Notion workspace…")
    all_pages = search_all_pages()
    print(f"  {len(all_pages)} page(s) visible to the integration")

    roots = []
    for p in all_pages:
        if p.get("parent", {}).get("type") != "workspace":
            continue
        title = get_title(p)
        try:
            flat = get_block_children(p["id"])
        except Exception as e:
            print(f"  WARNING: could not read top-level page {title!r}: {e}")
            continue
        heading = find_index_heading(flat)
        if not heading:
            continue
        leaves = [(b["id"], b["child_page"]["title"]) for b in flat if b["type"] == "child_page"]
        if not leaves:
            continue
        kind = classify_kind(heading)
        roots.append({"title": title, "kind": kind, "leaves": leaves})
        print(f'  Root page: "{title}" -> {kind} ({len(leaves)} sub-page(s))')

    if not roots:
        print("No root/index pages found -- nothing to sync.")
        sys.exit(1)

    machines_data = []
    labs_data, wargames_data = {}, {}

    for root in roots:
        if root["kind"] == "machines":
            for leaf_id, leaf_title in root["leaves"]:
                try:
                    machines_data.append(sync_leaf(leaf_id, leaf_title, "machines"))
                    print(f"    synced: {leaf_title}")
                except Exception as e:
                    print(f"    ERROR syncing {leaf_title!r}: {e}", file=sys.stderr)
            continue

        cat_slug = slugify(split_title(root["title"])[0]) or slugify(root["title"])
        label = clean_segment(split_title(root["title"])[0]) or root["title"]
        icon = extract_icon(root["title"])
        bucket = labs_data if root["kind"] == "labs" else wargames_data
        bucket.setdefault(cat_slug, {"label": label, "icon": icon, "leaves": [], "href": ""})

        for leaf_id, leaf_title in root["leaves"]:
            try:
                entry = sync_leaf(leaf_id, leaf_title, root["kind"], cat_slug=cat_slug, cat_label=label)
                bucket[cat_slug]["leaves"].append(entry)
                print(f"    synced: {leaf_title}")
            except Exception as e:
                print(f"    ERROR syncing {leaf_title!r}: {e}", file=sys.stderr)

        href = write_category_index(root["kind"], cat_slug, label, icon, bucket[cat_slug]["leaves"])
        bucket[cat_slug]["href"] = href

    print("Regenerating index.html…")
    update_index_html(machines_data, labs_data, wargames_data)

    print("Pruning orphaned files…")
    prune_orphans()

    print("\n✅ Sync complete.")
    print(f"   Machines: {len(machines_data)}")
    print(f"   Labs categories: {len(labs_data)} ({sum(len(b['leaves']) for b in labs_data.values())} labs)")
    print(f"   Wargames categories: {len(wargames_data)} ({sum(len(b['leaves']) for b in wargames_data.values())} levels)")


if __name__ == "__main__":
    main()
