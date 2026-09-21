import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
LOGO_DIR = ROOT / "logo"

# GitHub Actions protection limits.
# The script will stop NEW logo downloads after either limit is reached.
MAX_NEW_LOGOS_PER_RUN = 200
MAX_RUNTIME_SECONDS = 240          # Leave some margin before the 5-minute job timeout.
MAX_LOGO_RETRIES = 2
DELAY_BETWEEN_NEW_DOWNLOADS = 0.35

PLAYLIST_URLS = {
    "main.m3u": "https://raw.githubusercontent.com/amin8453/playlist/main/main.m3u",
    "india.m3u": "https://raw.githubusercontent.com/amin8453/playlist/main/india.m3u",
}

CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
LOGOS_URL = "https://iptv-org.github.io/api/logos.json"

START = time.monotonic()
session = requests.Session()
session.headers.update({"User-Agent": "worldtv-playlist-sync/4.0"})

new_logos = 0
cached_logos = 0
no_match = 0
download_failures = 0
limit_reached = False


def time_exceeded():
    return time.monotonic() - START >= MAX_RUNTIME_SECONDS


def safe_filename(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or "logo"


def normalize(value):
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"[\[\](){}]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def get_attr(line, name):
    m = re.search(rf'{re.escape(name)}="([^"]*)"', line, flags=re.I)
    return m.group(1).strip() if m else ""


def display_name_from_extinf(line):
    if "," in line:
        return line.split(",", 1)[1].strip()
    return ""


def existing_logo(base_name):
    """Return an already committed logo without downloading anything."""
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".avif"):
        p = LOGO_DIR / (base_name + ext)
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def extension_from_url(url):
    path = urlparse(url).path.lower()
    ext = Path(path).suffix
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".avif"}:
        return ext
    return ".png"


def fetch_json(url):
    r = session.get(url, timeout=20)
    r.raise_for_status()
    return r.json()


def build_indexes(channels, logos):
    channels_by_id = {}
    channels_by_name = {}

    for ch in channels:
        cid = str(ch.get("id", "")).strip()
        name = str(ch.get("name", "")).strip()

        if cid:
            channels_by_id[cid.lower()] = ch
        if name:
            channels_by_name.setdefault(normalize(name), []).append(ch)

    logos_by_channel = {}
    for logo in logos:
        cid = str(logo.get("channel", "")).strip().lower()
        url = str(logo.get("url", "")).strip()
        if cid and url:
            logos_by_channel.setdefault(cid, []).append(logo)

    return channels_by_id, channels_by_name, logos_by_channel


def choose_logo(candidates):
    if not candidates:
        return None

    def score(item):
        # Prefer active/in-use logos and common static formats.
        in_use = 1 if item.get("in_use") else 0
        fmt = str(item.get("format", "")).lower()
        fmt_score = 2 if fmt in ("png", "svg") else 1
        feed = 1 if item.get("feed") else 0
        return (in_use, -feed, fmt_score)

    return sorted(candidates, key=score, reverse=True)[0]


def find_channel(channel_id, display_name, channels_by_id, channels_by_name):
    if channel_id:
        ch = channels_by_id.get(channel_id.lower())
        if ch:
            return ch

    n = normalize(display_name)
    exact = channels_by_name.get(n)
    if exact:
        return exact[0]

    # Conservative fuzzy matching only.
    if not n:
        return None

    best = None
    best_score = 0
    second = 0

    for key, items in channels_by_name.items():
        score = fuzz.ratio(n, key)
        if score > best_score:
            second = best_score
            best_score = score
            best = items[0]
        elif score > second:
            second = score

    if best and best_score >= 92 and best_score - second >= 3:
        return best

    return None


def download_logo(url, destination):
    global new_logos, download_failures, limit_reached

    if destination.exists() and destination.stat().st_size > 0:
        return True

    if new_logos >= MAX_NEW_LOGOS_PER_RUN or time_exceeded():
        limit_reached = True
        return False

    last_error = None

    for attempt in range(MAX_LOGO_RETRIES):
        if time_exceeded():
            limit_reached = True
            return False

        try:
            r = session.get(url, timeout=10)

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = min(float(retry_after), 8.0)
                    except ValueError:
                        delay = 2.0
                else:
                    delay = 2.0 * (attempt + 1)

                # Do not spend the whole Actions run sleeping.
                if time.monotonic() - START + delay >= MAX_RUNTIME_SECONDS:
                    limit_reached = True
                    return False

                print(f"429 rate limit; waiting {delay:.1f}s before retry")
                time.sleep(delay)
                continue

            r.raise_for_status()

            content = r.content
            if not content:
                raise RuntimeError("empty response")

            destination.write_bytes(content)
            new_logos += 1

            time.sleep(DELAY_BETWEEN_NEW_DOWNLOADS)
            return True

        except Exception as exc:
            last_error = exc
            if attempt + 1 < MAX_LOGO_RETRIES:
                delay = 1.0 * (attempt + 1)
                if time.monotonic() - START + delay >= MAX_RUNTIME_SECONDS:
                    limit_reached = True
                    return False
                time.sleep(delay)

    download_failures += 1
    print(f"Logo failed: {url} -> {last_error}")
    return False


def logo_url_for(channel, logos_by_channel):
    cid = str(channel.get("id", "")).strip().lower()
    logo = choose_logo(logos_by_channel.get(cid, []))
    return str(logo.get("url", "")).strip() if logo else ""


def process_playlist(filename, source_url, channels_by_id, channels_by_name, logos_by_channel):
    global cached_logos, no_match, limit_reached

    print(f"\nSyncing {filename}...")

    r = session.get(source_url, timeout=30)
    r.raise_for_status()

    # Normalize literal escaped newlines that may occur in upstream data.
    text = r.text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n")
    lines = text.splitlines()

    out = []
    current_channel = None

    for line in lines:
        if time_exceeded() and new_logos >= MAX_NEW_LOGOS_PER_RUN:
            # We still rewrite the playlist using cached logos, but no new downloads.
            limit_reached = True

        if line.startswith("#EXTINF"):
            display_name = display_name_from_extinf(line)
            channel_id = get_attr(line, "tvg-id")

            current_channel = find_channel(
                channel_id,
                display_name,
                channels_by_id,
                channels_by_name,
            )

            if current_channel:
                base = safe_filename(str(current_channel.get("id") or display_name))
                cached = existing_logo(base)

                if cached:
                    cached_logos += 1
                    public_url = (
                        "https://cdn.jsdelivr.net/gh/heartlog/worldtv@main/logo/"
                        + cached.name
                    )
                    line = re.sub(
                        r'tvg-logo="[^"]*"',
                        f'tvg-logo="{public_url}"',
                        line,
                        flags=re.I,
                    )
                    if 'tvg-logo="' not in line:
                        line = line.replace(
                            "#EXTINF:",
                            f'#EXTINF:-1 tvg-logo="{public_url}"',
                            1,
                        )
                else:
                    url = logo_url_for(current_channel, logos_by_channel)

                    if url and not limit_reached and not time_exceeded() and new_logos < MAX_NEW_LOGOS_PER_RUN:
                        ext = extension_from_url(url)
                        destination = LOGO_DIR / (base + ext)

                        if download_logo(url, destination):
                            public_url = (
                                "https://cdn.jsdelivr.net/gh/heartlog/worldtv@main/logo/"
                                + destination.name
                            )
                            line = re.sub(
                                r'tvg-logo="[^"]*"',
                                f'tvg-logo="{public_url}"',
                                line,
                                flags=re.I,
                            )
                            if 'tvg-logo="' not in line:
                                line = line.replace(
                                    "#EXTINF:",
                                    f'#EXTINF:-1 tvg-logo="{public_url}"',
                                    1,
                                )
                        else:
                            if time_exceeded() or new_logos >= MAX_NEW_LOGOS_PER_RUN:
                                limit_reached = True
                    elif url:
                        limit_reached = True
                    else:
                        no_match += 1

        out.append(line)

    # Ensure a normal M3U header and actual newlines.
    while out and not out[0].strip():
        out.pop(0)

    if not out or out[0].strip().upper() != "#EXTM3U":
        out.insert(0, "#EXTM3U")
    else:
        out[0] = "#EXTM3U"

    (ROOT / filename).write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def main():
    LOGO_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading IPTV-org databases...")
    channels = fetch_json(CHANNELS_URL)
    logos = fetch_json(LOGOS_URL)

    channels_by_id, channels_by_name, logos_by_channel = build_indexes(channels, logos)

    print(f"Channels indexed: {len(channels_by_id)}")
    print(f"Logo channel IDs indexed: {len(logos_by_channel)}")
    print(f"Limits: {MAX_NEW_LOGOS_PER_RUN} new logos / {MAX_RUNTIME_SECONDS}s")

    for filename, url in PLAYLIST_URLS.items():
        # Do not start another playlist download if the hard runtime is already reached.
        if time_exceeded():
            print("Runtime limit reached; stopping.")
            break

        process_playlist(
            filename,
            url,
            channels_by_id,
            channels_by_name,
            logos_by_channel,
        )

    elapsed = time.monotonic() - START

    print("\n=== Summary ===")
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"New logos downloaded: {new_logos}")
    print(f"Logos from cache: {cached_logos}")
    print(f"No logo match: {no_match}")
    print(f"Download failures: {download_failures}")
    print(f"Limit reached: {limit_reached}")

    if limit_reached:
        print(
            "Download limit/runtime limit reached. "
            "The next scheduled run will continue with missing logos."
        )


if __name__ == "__main__":
    main()
