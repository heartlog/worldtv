#!/usr/bin/env python3
import hashlib
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

import requests

UPSTREAM_RAW = "https://raw.githubusercontent.com/amin8453/playlist/main/"
IPTV_API = "https://iptv-org.github.io/api/channels.json"
IPTV_LOGO_BASE = "https://logos-world.net/wp-content/uploads/"  # fallback is not used automatically

ROOT = Path(__file__).resolve().parent.parent
LOGO_DIR = ROOT / "logo"
LOGO_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "worldtv-playlist-sync/1.0"
}

session = requests.Session()
session.headers.update(HEADERS)


def download(url, timeout=30):
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r


def sync_playlist(filename):
    print(f"Downloading {filename}...")
    data = download(UPSTREAM_RAW + filename).content
    (ROOT / filename).write_bytes(data)


def load_channels():
    print("Loading IPTV-org channel database...")
    r = download(IPTV_API, timeout=60)
    return r.json()


def normalize(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def safe_filename(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    return value.strip("._")[:180] or "channel"


def image_extension(url, content_type=""):
    path = urlparse(url).path.lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
        if path.endswith(ext):
            return ext
    if "svg" in content_type.lower():
        return ".svg"
    if "webp" in content_type.lower():
        return ".webp"
    if "jpeg" in content_type.lower() or "jpg" in content_type.lower():
        return ".jpg"
    return ".png"


def find_logo(channel):
    # IPTV-org's channels.json normally has a "logo" URL.
    logo = channel.get("logo")
    if logo:
        return logo

    return None


def build_indexes(channels):
    by_id = {}
    by_name = {}

    for ch in channels:
        cid = normalize(ch.get("id"))
        name = normalize(ch.get("name"))

        if cid:
            by_id[cid] = ch
        if name:
            by_name.setdefault(name, []).append(ch)

    return by_id, by_name


def process_main_playlist(channels):
    by_id, by_name = build_indexes(channels)

    path = ROOT / "main.m3u"
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    output = []
    current_logo = None
    downloaded = 0
    missing = 0

    for line in lines:
        if line.startswith("#EXTINF:"):
            # Capture tvg-id and channel display name.
            id_match = re.search(r'tvg-id="([^"]*)"', line)
            name = line.split(",", 1)[1].strip() if "," in line else ""

            tvg_id = id_match.group(1).strip() if id_match else ""

            channel = None

            if tvg_id:
                channel = by_id.get(normalize(tvg_id))

            # Conservative fallback: exact normalized display name.
            if channel is None and name:
                matches = by_name.get(normalize(name), [])
                if len(matches) == 1:
                    channel = matches[0]

            logo_url = find_logo(channel) if channel else None

            if logo_url:
                filename_base = safe_filename(tvg_id or channel.get("id") or name)
                # Keep the URL extension when possible.
                try:
                    response = download(logo_url)
                    ext = image_extension(
                        logo_url,
                        response.headers.get("Content-Type", "")
                    )
                    logo_path = LOGO_DIR / f"{filename_base}{ext}"
                    logo_path.write_bytes(response.content)

                    jsdelivr_url = (
                        "https://cdn.jsdelivr.net/gh/heartlog/worldtv@main/"
                        f"logo/{logo_path.name}"
                    )

                    # Replace existing tvg-logo, or add it.
                    if 'tvg-logo="' in line:
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

                    current_logo = logo_path
                    downloaded += 1
                except Exception as exc:
                    print(f"Logo download failed for {name!r}: {exc}")
                    current_logo = None
                    missing += 1
            else:
                current_logo = None
                missing += 1

        output.append(line)

    path.write_text("\n".join(output) + "\n", encoding="utf-8")

    print(f"Logos downloaded: {downloaded}")
    print(f"Channels without a matched logo: {missing}")


def main():
    sync_playlist("main.m3u")
    sync_playlist("india.m3u")

    channels = load_channels()
    process_main_playlist(channels)


if __name__ == "__main__":
    main()
