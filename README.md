# Waseef — Cybersecurity Portfolio

Live site: [mostafaelwaseef.github.io](https://mostafaelwaseef.github.io)

A personal portfolio documenting hands-on penetration testing work: CTF machines (TryHackMe, VulnHub), PortSwigger Web Security Academy labs, and OverTheWire wargames.

## How this site works

All write-ups are authored in Notion and synced to this repository automatically — there is no manual copy/paste and no step where anyone needs to "deploy" the site.

```
Notion (source of truth)
   │
   │  scheduled GitHub Action, weekly (or run manually anytime)
   ▼
.github/scripts/sync_notion.py
   │
   │  discovers every "index" page in the workspace, renders each
   │  sub-page to static HTML, downloads every image locally
   ▼
writeups/{machines,labs,wargames}/...   +   index.html sections
```

**Fully self-contained output.** Every generated page is plain HTML/CSS with no dependency on Notion at runtime — no embeds, no iframes, no live links back to Notion. Images are downloaded once and committed to the repo.

**Zero-maintenance discovery.** The sync script doesn't hardcode which pages to pull. It searches the whole Notion workspace and treats any top-level page containing an "🗂️ ... Index" heading as a category (Machines Index, Labs Index, Levels Index, etc.), then syncs every sub-page beneath it. Add a new machine write-up, lab, or wargame level in Notion under an existing category and it appears on the site on the next sync — no code changes needed. A brand-new *category* (e.g. a new wargame) just needs to be shared with the Notion integration once.

## Structure

- `index.html` — landing page. The Machines / Labs / Wargames sections are wrapped in `<!-- AUTO:...:START/END -->` markers and rewritten by the sync script; everything else (hero, about, contact) is hand-authored and left untouched.
- `writeups/machines/` — flat CTF machine write-ups.
- `writeups/labs/{category}/` — PortSwigger lab write-ups, grouped by vulnerability class, each with its own category index page.
- `writeups/wargames/{category}/` — OverTheWire wargame levels, grouped by wargame, each with its own category index page.
- `.github/scripts/sync_notion.py` — the sync/render logic.
- `.github/workflows/sync-notion.yml` — the scheduled GitHub Action (requires a `NOTION_TOKEN` repository secret).

## Tools & techniques covered

Nmap · Burp Suite · Metasploit · Gobuster · SQLmap · Netcat · Nikto · John the Ripper · Hashcat · Wireshark · Linux · Python · Bash
