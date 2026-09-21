#!/usr/bin/env python3
import re
from pathlib import Path
from urllib.parse import urlparse
import requests
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
LOGO_DIR = ROOT / "logo"
UPSTREAM = "https://raw.githubusercontent.com/amin8453/playlist/main/"
CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
LOGOS_URL = "https://iptv-org.github.io/api/logos.json"

s = requests.Session()
s.headers["User-Agent"] = "heartlog-worldtv-logo-sync/1.0"

def get(url, timeout=90):
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    return r

def norm(v):
    v = (v or "").lower()
    v = re.sub(r"\\b(fhd|uhd|hd|sd|4k|2k|1080p|720p|576p|480p)\\b", " ", v)
    v = re.sub(r"\\([^)]*\\)", " ", v)
    return re.sub(r"[^a-z0-9]+", "", v)

def norm_id(v):
    return re.sub(r"[^a-z0-9]+", "", (v or "").lower())

def safe(v):
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", v or "").strip("._") or "channel")[:180]

def ext(url, ct=""):
    p = urlparse(url).path.lower()
    for x in (".png",".jpg",".jpeg",".webp",".svg",".gif",".avif"):
        if p.endswith(x): return x
    ct = ct.lower()
    if "svg" in ct: return ".svg"
    if "webp" in ct: return ".webp"
    if "jpeg" in ct or "jpg" in ct: return ".jpg"
    if "gif" in ct: return ".gif"
    if "avif" in ct: return ".avif"
    return ".png"

def choose(items):
    if not items: return None
    def score(x):
        z = 100 if x.get("in_use") else 0
        z += 30 if x.get("feed") is None else 0
        z += 8 if (x.get("format") or "").upper() == "PNG" else 0
        z += 6 if (x.get("format") or "").upper() == "SVG" else 0
        return z
    return max(items, key=score)

def main():
    LOGO_DIR.mkdir(exist_ok=True)

    for name in ("main.m3u", "india.m3u"):
        print("Downloading", name)
        (ROOT/name).write_bytes(get(UPSTREAM+name).content)

    print("Loading IPTV-org channel database...")
    channels = get(CHANNELS_URL).json()
    print("Loading IPTV-org logo database...")
    logos = get(LOGOS_URL).json()

    by_id, by_name, logos_by_id = {}, {}, {}
    for c in channels:
        if not c.get("id"): continue
        by_id[norm_id(c["id"])] = c
        for n in [c.get("name")] + (c.get("alt_names") or []):
            if n: by_name.setdefault(norm(n), []).append(c)

    for x in logos:
        if x.get("channel") and x.get("url"):
            logos_by_id.setdefault(norm_id(x["channel"]), []).append(x)

    lines = (ROOT/"main.m3u").read_text(encoding="utf-8", errors="replace").splitlines()
    out, matched, downloaded, reused, missing, failed = [], 0, 0, 0, 0, 0

    # Always write a valid M3U header. The upstream file should contain
    # #EXTM3U, but normalize it in case the source changes.
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip().lower() in ("extm3u", "#extm3u"):
        lines[0] = "#EXTM3U"
    elif not lines or not lines[0].startswith("#EXTM3U"):
        lines.insert(0, "#EXTM3U")

    for line in lines:
        if not line.startswith("#EXTINF:"):
            out.append(line); continue

        m = re.search(r'tvg-id="([^"]*)"', line)
        tvg_id = m.group(1).strip() if m else ""
        display = line.split(",",1)[1].strip() if "," in line else ""

        channel = by_id.get(norm_id(tvg_id)) if tvg_id else None
        method = "id" if channel else None

        if not channel:
            candidates = by_name.get(norm(display), [])
            if len(candidates) == 1:
                channel, method = candidates[0], "name"

        if not channel and display:
            key = norm(display)
            best = second = 0
            best_channel = None
            for n, candidates in by_name.items():
                score = fuzz.ratio(key, n)
                if score > best:
                    second, best, best_channel = best, score, candidates[0]
                elif score > second:
                    second = score
            if best_channel and best >= 92 and best-second >= 3:
                channel, method = best_channel, "fuzzy"

        if not channel:
            missing += 1
            out.append(line); continue

        logo = choose(logos_by_id.get(norm_id(channel["id"]), []))
        if not logo:
            missing += 1
            out.append(line); continue

        base = safe(tvg_id or channel["id"] or display)
        existing = list(LOGO_DIR.glob(base+".*"))
        try:
            if existing:
                lp = existing[0]; reused += 1
            else:
                r = get(logo["url"], 45)
                lp = LOGO_DIR/(base+ext(logo["url"], r.headers.get("content-type","")))
                lp.write_bytes(r.content); downloaded += 1

            js = f"https://cdn.jsdelivr.net/gh/heartlog/worldtv@main/logo/{lp.name}"
            if 'tvg-logo="' in line:
                line = re.sub(r'tvg-logo="[^"]*"', f'tvg-logo="{js}"', line, count=1)
            else:
                line = line.replace("#EXTINF:-1", f'#EXTINF:-1 tvg-logo="{js}"', 1)
            matched += 1
        except Exception as e:
            failed += 1
            print("Logo failed:", display, e)

        out.append(line)

    (ROOT/"main.m3u").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\\nRESULT")
    print("Matched:", matched)
    print("Downloaded:", downloaded)
    print("Reused:", reused)
    print("No logo:", missing)
    print("Failed:", failed)

if __name__ == "__main__":
    main()
