#!/usr/bin/env python3

import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
LOGO_DIR = ROOT / "logo"

UPSTREAM = "https://raw.githubusercontent.com/amin8453/playlist/main/"
CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
LOGOS_URL = "https://iptv-org.github.io/api/logos.json"

session = requests.Session()
session.headers.update({
    "User-Agent": "heartlog-worldtv-logo-sync/1.0"
})

# Logo servers can rate-limit large playlists.
MAX_LOGO_RETRIES = 5
INITIAL_RETRY_DELAY = 3
DELAY_BETWEEN_NEW_DOWNLOADS = 0.35


def get(url, timeout=90, retries=3):
    """GET JSON/raw files with a small retry policy."""
    last_error = None

    for attempt in range(retries):
        try:
            r = session.get(url, timeout=timeout)

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else 5
                delay = min(delay, 120)
                print(f"429 while downloading {url}; waiting {delay}s")
                time.sleep(delay)
                continue

            r.raise_for_status()
            return r

        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)

    raise last_error


def get_logo(url):
    """Download a logo with 429-aware exponential backoff."""
    delay = INITIAL_RETRY_DELAY
    last_error = None

    for attempt in range(1, MAX_LOGO_RETRIES + 1):
        try:
            r = session.get(url, timeout=45)

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")

                if retry_after and retry_after.isdigit():
                    wait = min(int(retry_after), 180)
                else:
                    wait = min(delay, 180)

                print(
                    f"429 Too Many Requests for {url}; "
                    f"retry {attempt}/{MAX_LOGO_RETRIES} in {wait}s"
                )
                time.sleep(wait)
                delay *= 2
                continue

            r.raise_for_status()
            return r

        except requests.RequestException as exc:
            last_error = exc

            if attempt < MAX_LOGO_RETRIES:
                print(
                    f"Logo request failed; "
                    f"retry {attempt}/{MAX_LOGO_RETRIES} in {delay}s: {exc}"
                )
                time.sleep(delay)
                delay *= 2

    raise last_error


def norm(value):
    value = (value or "").lower()
    value = re.sub(
        r"\b(fhd|uhd|hd|sd|4k|2k|1080p|720p|576p|480p)\b",
        " ",
        value,
    )
    value = re.sub(r"\([^)]*\)", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def norm_id(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def safe(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    return (value.strip("._") or "channel")[:180]


def extension_for(url, content_type=""):
    path = urlparse(url).path.lower()

    for extension in (
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".svg",
        ".gif",
        ".avif",
    ):
        if path.endswith(extension):
            return extension

    content_type = content_type.lower()

    if "svg" in content_type:
        return ".svg"
    if "webp" in content_type:
        return ".webp"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    if "gif" in content_type:
        return ".gif"
    if "avif" in content_type:
        return ".avif"

    return ".png"


def choose_logo(items):
    if not items:
        return None

    def score(item):
        score_value = 100 if item.get("in_use") else 0
        score_value += 30 if item.get("feed") is None else 0

        fmt = (item.get("format") or "").upper()

        if fmt == "PNG":
            score_value += 8
        elif fmt == "SVG":
            score_value += 6

        return score_value

    return max(items, key=score)


def existing_logo(base_name):
    """Return an existing logo without downloading anything."""
    matches = []

    for extension in (
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".svg",
        ".gif",
        ".avif",
    ):
        candidate = LOGO_DIR / (base_name + extension)

        if candidate.is_file() and candidate.stat().st_size > 0:
            matches.append(candidate)

    return matches[0] if matches else None


def load_database():
    print("Loading IPTV-org channel database...")
    channels = get(CHANNELS_URL).json()

    print("Loading IPTV-org logo database...")
    logos = get(LOGOS_URL).json()

    by_id = {}
    by_name = {}
    logos_by_id = {}

    for channel in channels:
        channel_id = channel.get("id")

        if not channel_id:
            continue

        by_id[norm_id(channel_id)] = channel

        names = [channel.get("name")]
        names.extend(channel.get("alt_names") or [])

        for name in names:
            if name:
                by_name.setdefault(norm(name), []).append(channel)

    for logo in logos:
        channel_id = logo.get("channel")
        logo_url = logo.get("url")

        if channel_id and logo_url:
            logos_by_id.setdefault(
                norm_id(channel_id),
                [],
            ).append(logo)

    return by_id, by_name, logos_by_id


def find_channel(tvg_id, display_name, by_id, by_name):
    # 1. Safest: exact tvg-id.
    if tvg_id:
        channel = by_id.get(norm_id(tvg_id))

        if channel:
            return channel

    # 2. Exact display-name match.
    if display_name:
        candidates = by_name.get(norm(display_name), [])

        if len(candidates) == 1:
            return candidates[0]

    # 3. Conservative fuzzy matching.
    if not display_name:
        return None

    key = norm(display_name)

    best_score = 0
    second_score = 0
    best_channel = None

    for candidate_name, candidates in by_name.items():
        score = fuzz.ratio(key, candidate_name)

        if score > best_score:
            second_score = best_score
            best_score = score
            best_channel = candidates[0]
        elif score > second_score:
            second_score = score

    if (
        best_channel
        and best_score >= 92
        and best_score - second_score >= 3
    ):
        return best_channel

    return None


def process_main(by_id, by_name, logos_by_id):
    path = ROOT / "main.m3u"

    lines = path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines()

    # Normalize the playlist header.
    while lines and not lines[0].strip():
        lines.pop(0)

    if lines and lines[0].strip().lower() in ("extm3u", "#extm3u"):
        lines[0] = "#EXTM3U"
    elif not lines or not lines[0].startswith("#EXTM3U"):
        lines.insert(0, "#EXTM3U")

    output = []

    matched = 0
    downloaded = 0
    cached = 0
    missing = 0
    failed = 0

    for line in lines:
        if not line.startswith("#EXTINF:"):
            output.append(line)
            continue

        id_match = re.search(
            r'tvg-id="([^"]*)"',
            line,
        )

        logo_match = re.search(
            r'tvg-logo="([^"]*)"',
            line,
        )

        tvg_id = id_match.group(1).strip() if id_match else ""
        display_name = (
            line.split(",", 1)[1].strip()
            if "," in line
            else ""
        )

        channel = find_channel(
            tvg_id,
            display_name,
            by_id,
            by_name,
        )

        if not channel:
            missing += 1
            output.append(line)
            continue

        logo = choose_logo(
            logos_by_id.get(
                norm_id(channel["id"]),
                [],
            )
        )

        if not logo:
            missing += 1
            output.append(line)
            continue

        base_name = safe(
            tvg_id
            or channel.get("id")
            or display_name
        )

        # IMPORTANT:
        # Never download a logo that is already committed to logo/.
        logo_path = existing_logo(base_name)

        try:
            if logo_path:
                cached += 1
                print(f"CACHED: {display_name} -> {logo_path.name}")
            else:
                print(f"DOWNLOAD: {display_name} -> {logo['url']}")

                response = get_logo(logo["url"])

                logo_path = LOGO_DIR / (
                    base_name
                    + extension_for(
                        logo["url"],
                        response.headers.get(
                            "Content-Type",
                            "",
                        ),
                    )
                )

                logo_path.write_bytes(response.content)

                downloaded += 1

                # Slow down only after a NEW download.
                time.sleep(DELAY_BETWEEN_NEW_DOWNLOADS)

            jsdelivr_url = (
                "https://cdn.jsdelivr.net/gh/"
                "heartlog/worldtv@main/"
                f"logo/{logo_path.name}"
            )

            if logo_match:
                line = re.sub(
                    r'tvg-logo="[^"]*"',
                    f'tvg-logo="{jsdelivr_url}"',
                    line,
                    count=1,
                )
            else:
                line = line.replace(
                    "#EXTINF:-1",
                    f'#EXTINF:-1 tvg-logo="{jsdelivr_url}"',
                    1,
                )

            matched += 1

        except Exception as exc:
            failed += 1
            print(
                f"Logo failed: {display_name}: {exc}"
            )

            # Don't destroy an existing valid logo if the new
            # logo request failed.
            if logo_match and logo_match.group(1).strip():
                pass

        output.append(line)

    # Actual newline characters are deliberately used here.
    path.write_text(
        "\n".join(output) + "\n",
        encoding="utf-8",
    )

    print()
    print("========== RESULT ==========")
    print(f"Matched:              {matched}")
    print(f"New logos downloaded: {downloaded}")
    print(f"Logos from cache:     {cached}")
    print(f"No logo match:        {missing}")
    print(f"Download failures:    {failed}")
    print("============================")


def main():
    LOGO_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Always get the newest upstream playlists.
    for filename in ("main.m3u", "india.m3u"):
        print(f"Downloading {filename}...")
        response = get(UPSTREAM + filename)
        (ROOT / filename).write_bytes(response.content)

    by_id, by_name, logos_by_id = load_database()

    process_main(
        by_id,
        by_name,
        logos_by_id,
    )


if __name__ == "__main__":
    main()
