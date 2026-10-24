#!/usr/bin/env python3
"""
GTA live stream finder.

Checks a list of Twitch, YouTube and Kick channels and reports the ones that
are live with GTA content (category or title match). Results are printed and,
optionally, posted to a Discord webhook.

Usage:
    python3 gta_live_finder.py                 # loop forever, check every 5 min
    python3 gta_live_finder.py --once          # check once and exit
    python3 gta_live_finder.py --once --no-filter   # list ALL live channels (for testing keys)

Keys are read from environment variables (see README section in chat):
    TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
    YOUTUBE_API_KEY
    KICK_CLIENT_ID, KICK_CLIENT_SECRET
    DISCORD_WEBHOOK_URL
Any platform whose keys are missing is skipped.
"""

import argparse
import json
import os
import re
import sys
import time

import requests

TIMEOUT = 15
GTA_PATTERN = re.compile(r"\bgta|grand theft auto|vice city", re.IGNORECASE)
SEEN_FILE = "seen.json"
NO_FILTER = False


def is_gta(*texts):
    if NO_FILTER:
        return True
    blob = " ".join(t or "" for t in texts)
    return bool(GTA_PATTERN.search(blob))


# ----------------------------------------------------------------- Twitch --
def check_twitch(logins):
    cid = os.getenv("TWITCH_CLIENT_ID")
    secret = os.getenv("TWITCH_CLIENT_SECRET")
    if not (cid and secret and logins):
        return []

    tok = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": cid,
            "client_secret": secret,
            "grant_type": "client_credentials",
        },
        timeout=TIMEOUT,
    )
    tok.raise_for_status()
    headers = {
        "Client-Id": cid,
        "Authorization": f"Bearer {tok.json()['access_token']}",
    }

    found = []
    for i in range(0, len(logins), 100):  # API allows up to 100 per request
        batch = logins[i : i + 100]
        params = [("user_login", name) for name in batch] + [("first", 100)]
        r = requests.get(
            "https://api.twitch.tv/helix/streams",
            headers=headers,
            params=params,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        for s in r.json().get("data", []):
            if s.get("type") != "live":
                continue
            if is_gta(s.get("game_name"), s.get("title")):
                found.append(
                    {
                        "platform": "Twitch",
                        "name": s["user_name"],
                        "title": s.get("title", ""),
                        "game": s.get("game_name", ""),
                        "viewers": s.get("viewer_count", 0),
                        "url": f"https://twitch.tv/{s['user_login']}",
                    }
                )
    return found


# ---------------------------------------------------------------- YouTube --
def check_youtube(channels):
    """channels: list of {"name": "...", "id": "UC..."}.

    Uses each channel's uploads playlist (1 quota unit) instead of search
    (100 units), then checks which recent videos are currently live.
    """
    key = os.getenv("YOUTUBE_API_KEY")
    channels = [c for c in channels if c.get("id", "").startswith("UC")]
    if not (key and channels):
        return []

    video_owner = {}
    for ch in channels:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            params={
                "part": "contentDetails",
                "playlistId": "UU" + ch["id"][2:],
                "maxResults": 5,
                "key": key,
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[YouTube] {ch.get('name')}: HTTP {r.status_code}", file=sys.stderr)
            continue
        for item in r.json().get("items", []):
            video_owner[item["contentDetails"]["videoId"]] = ch["name"]

    found = []
    ids = list(video_owner)
    for i in range(0, len(ids), 50):
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "snippet,liveStreamingDetails",
                "id": ",".join(ids[i : i + 50]),
                "key": key,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        for v in r.json().get("items", []):
            sn = v["snippet"]
            if sn.get("liveBroadcastContent") != "live":
                continue
            if is_gta(sn.get("title"), sn.get("description", "")[:300]):
                found.append(
                    {
                        "platform": "YouTube",
                        "name": video_owner[v["id"]],
                        "title": sn.get("title", ""),
                        "game": "",
                        "viewers": v.get("liveStreamingDetails", {}).get(
                            "concurrentViewers", "?"
                        ),
                        "url": f"https://www.youtube.com/watch?v={v['id']}",
                    }
                )
    return found


# ------------------------------------------------------------------- Kick --
def check_kick(slugs):
    cid = os.getenv("KICK_CLIENT_ID")
    secret = os.getenv("KICK_CLIENT_SECRET")
    if not (cid and secret and slugs):
        return []

    tok = requests.post(
        "https://id.kick.com/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": secret,
        },
        timeout=TIMEOUT,
    )
    tok.raise_for_status()
    headers = {"Authorization": f"Bearer {tok.json()['access_token']}"}

    found = []
    for i in range(0, len(slugs), 50):
        batch = slugs[i : i + 50]
        r = requests.get(
            "https://api.kick.com/public/v1/channels",
            headers=headers,
            params=[("slug", s) for s in batch],
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        for c in r.json().get("data", []):
            stream = c.get("stream") or {}
            if not stream.get("is_live"):
                continue
            category = (c.get("category") or {}).get("name", "")
            title = c.get("stream_title", "")
            if is_gta(category, title):
                found.append(
                    {
                        "platform": "Kick",
                        "name": c.get("slug"),
                        "title": title,
                        "game": category,
                        "viewers": stream.get("viewer_count", 0),
                        "url": f"https://kick.com/{c.get('slug')}",
                    }
                )
    return found


# ---------------------------------------------------------------- Output ---
def format_line(s):
    game = f" [{s['game']}]" if s["game"] else ""
    return (
        f"🔴 **{s['platform']}** - {s['name']}{game} "
        f"({s['viewers']} viewers)\n{s['title']}\n{s['url']}"
    )


def post_discord(lines):
    hook = os.getenv("DISCORD_WEBHOOK_URL")
    if not hook or not lines:
        return
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 2 > 1900:
            requests.post(hook, json={"content": chunk}, timeout=TIMEOUT)
            chunk = ""
            time.sleep(1)
        chunk += line + "\n\n"
    if chunk:
        requests.post(hook, json={"content": chunk}, timeout=TIMEOUT)


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_seen(seen):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(sorted(seen), f)
    except OSError:
        pass


def run_once(cfg, seen):
    results = []
    for label, fn, arg in (
        ("Twitch", check_twitch, cfg.get("twitch", [])),
        ("YouTube", check_youtube, cfg.get("youtube", [])),
        ("Kick", check_kick, cfg.get("kick", [])),
    ):
        try:
            results += fn(arg)
        except Exception as e:  # keep going if one platform fails
            print(f"[{label}] error: {e}", file=sys.stderr)

    results.sort(key=lambda s: str(s["viewers"]).zfill(10), reverse=True)

    live_urls = {s["url"] for s in results}
    new = [s for s in results if s["url"] not in seen]

    print(f"\n=== {time.strftime('%H:%M:%S')} | {len(results)} live, {len(new)} new ===")
    for s in results:
        print(format_line(s).replace("**", "") + "\n")

    post_discord([format_line(s) for s in new])

    # Remember what is live now; streams that go offline get forgotten so a
    # later stream by the same person is announced again.
    seen.clear()
    seen.update(live_urls)
    save_seen(seen)


def main():
    global NO_FILTER
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--interval", type=int, default=300, help="seconds between checks")
    ap.add_argument(
        "--no-filter",
        action="store_true",
        help="show every live channel, not just GTA (for testing)",
    )
    args = ap.parse_args()
    NO_FILTER = args.no_filter

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    seen = load_seen()
    while True:
        run_once(cfg, seen)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
