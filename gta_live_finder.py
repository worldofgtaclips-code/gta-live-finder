#!/usr/bin/env python3
"""
GTA stream finder (live + recently ended).

Looks at a list of Twitch, YouTube and Kick channels and reports GTA streams
that were live or ended within the last N hours, with a link for each
(a VOD/replay link for ended streams, the channel link for live ones).
Results are printed and, optionally, posted to a Discord webhook.

Usage:
    python3 gta_live_finder.py --once                   # one check, 6-hour window
    python3 gta_live_finder.py --once --window-hours 12
    python3 gta_live_finder.py --once --no-filter       # ignore the GTA filter (testing)

Environment variables (platforms without keys are skipped):
    TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
    YOUTUBE_API_KEY
    KICK_CLIENT_ID, KICK_CLIENT_SECRET
    DISCORD_WEBHOOK_URL

Platform limits:
    Twitch  - live streams (by category or title) + past broadcasts (by title only,
              because Twitch does not expose the game for past broadcasts).
              Needs the streamer to have VODs enabled.
    YouTube - live streams and ended live streams (by title/description).
    Kick    - only streams that are live at the moment the script runs
              (Kick's official API has no replay listing).
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

TIMEOUT = 15
GTA_PATTERN = re.compile(r"\bgta|grand theft auto|vice city", re.IGNORECASE)
SEEN_FILE = "seen.json"
SEEN_TTL = 7 * 24 * 3600  # forget announced items after 7 days
NO_FILTER = False
DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


# --------------------------------------------------------------- helpers ---
def getenv(name):
    """Read an environment variable and strip stray spaces/newlines (a common
    copy-paste mistake when saving secrets)."""
    return (os.getenv(name) or "").strip()


def is_gta(*texts):
    if NO_FILTER:
        return True
    blob = " ".join(t or "" for t in texts)
    return bool(GTA_PATTERN.search(blob))


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_duration(value):
    """Twitch durations look like '3h12m5s'."""
    m = DURATION_RE.fullmatch(value or "")
    if not m:
        return timedelta(0)
    h, mi, sec = (int(x or 0) for x in m.groups())
    return timedelta(hours=h, minutes=mi, seconds=sec)


def fmt_duration(td):
    total = int(td.total_seconds())
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


# ----------------------------------------------------------------- Twitch --
def twitch_headers():
    cid = getenv("TWITCH_CLIENT_ID")
    secret = getenv("TWITCH_CLIENT_SECRET")
    if not (cid and secret):
        return None
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
    return {"Client-Id": cid, "Authorization": f"Bearer {tok.json()['access_token']}"}


def check_twitch(logins, window_start):
    headers = twitch_headers()
    if not (headers and logins):
        return []

    results = []
    live_stream_ids = set()

    # 1) Streams live right now (filtered by category or title)
    for i in range(0, len(logins), 100):
        params = [("user_login", n) for n in logins[i : i + 100]] + [("first", 100)]
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
            live_stream_ids.add(s["id"])
            if is_gta(s.get("game_name"), s.get("title")):
                results.append(
                    {
                        "platform": "Twitch",
                        "name": s["user_name"],
                        "title": s.get("title", ""),
                        "game": s.get("game_name", ""),
                        "status": "LIVE",
                        "started": parse_iso(s.get("started_at")),
                        "duration": None,
                        "viewers": s.get("viewer_count", 0),
                        "url": f"https://twitch.tv/{s['user_login']}",
                        "key": f"twitch-live:{s['id']}",
                    }
                )

    print(f"[Twitch] checked {len(logins)} channels, {len(live_stream_ids)} live right now")

    # 2) Past broadcasts (VODs) that ended inside the window
    users = []
    for i in range(0, len(logins), 100):
        params = [("login", n) for n in logins[i : i + 100]]
        r = requests.get(
            "https://api.twitch.tv/helix/users",
            headers=headers,
            params=params,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        users += r.json().get("data", [])

    for u in users:
        r = requests.get(
            "https://api.twitch.tv/helix/videos",
            headers=headers,
            params={"user_id": u["id"], "type": "archive", "first": 10},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[Twitch] videos for {u['login']}: HTTP {r.status_code}", file=sys.stderr)
            continue
        for v in r.json().get("data", []):
            if v.get("stream_id") in live_stream_ids:
                continue  # still live, already covered above
            start = parse_iso(v.get("created_at"))
            if not start:
                continue
            duration = parse_duration(v.get("duration"))
            if start + duration < window_start:
                continue  # ended before the window
            if not is_gta(v.get("title")):
                continue
            results.append(
                {
                    "platform": "Twitch",
                    "name": u["display_name"],
                    "title": v.get("title", ""),
                    "game": "",
                    "status": "ENDED",
                    "started": start,
                    "duration": duration,
                    "viewers": v.get("view_count", 0),
                    "url": v["url"],
                    "key": f"twitch-vod:{v['id']}",
                }
            )
    return results


# ---------------------------------------------------------------- YouTube --
def check_youtube(channels, window_start):
    """channels: list of {"name": "...", "id": "UC..."}."""
    key = getenv("YOUTUBE_API_KEY")
    channels = [
        c for c in channels
        if c.get("id", "").startswith("UC") and "xxxx" not in c.get("id", "").lower()
    ]
    if not (key and channels):
        return []

    video_owner = {}
    for ch in channels:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            params={
                "part": "contentDetails",
                "playlistId": "UU" + ch["id"][2:],  # uploads playlist
                "maxResults": 10,
                "key": key,
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[YouTube] {ch.get('name')}: HTTP {r.status_code}", file=sys.stderr)
            continue
        for item in r.json().get("items", []):
            video_owner[item["contentDetails"]["videoId"]] = ch["name"]

    results = []
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
            det = v.get("liveStreamingDetails")
            if not det:
                continue  # not a live stream / premiere
            sn = v["snippet"]
            if not is_gta(sn.get("title"), sn.get("description", "")[:300]):
                continue
            start = parse_iso(det.get("actualStartTime"))
            end = parse_iso(det.get("actualEndTime"))
            if sn.get("liveBroadcastContent") == "live":
                status, duration = "LIVE", None
                viewers = det.get("concurrentViewers", "?")
            elif end and end >= window_start:
                status = "ENDED"
                duration = (end - start) if start else None
                viewers = "?"
            else:
                continue
            results.append(
                {
                    "platform": "YouTube",
                    "name": video_owner[v["id"]],
                    "title": sn.get("title", ""),
                    "game": "",
                    "status": status,
                    "started": start,
                    "duration": duration,
                    "viewers": viewers,
                    "url": f"https://www.youtube.com/watch?v={v['id']}",
                    "key": f"youtube:{v['id']}",
                }
            )
    return results


# ------------------------------------------------------------------- Kick --
def check_kick(slugs, window_start):  # window_start unused: live only
    cid = getenv("KICK_CLIENT_ID")
    secret = getenv("KICK_CLIENT_SECRET")
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

    results = []
    returned = live_now = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for i in range(0, len(slugs), 50):
        r = requests.get(
            "https://api.kick.com/public/v1/channels",
            headers=headers,
            params=[("slug", s) for s in slugs[i : i + 50]],
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        for c in r.json().get("data", []):
            returned += 1
            stream = c.get("stream") or {}
            if not stream.get("is_live"):
                continue
            live_now += 1
            category = (c.get("category") or {}).get("name", "")
            title = c.get("stream_title", "")
            if not is_gta(category, title):
                continue
            start = parse_iso(stream.get("start_time"))
            results.append(
                {
                    "platform": "Kick",
                    "name": c.get("slug"),
                    "title": title,
                    "game": category,
                    "status": "LIVE",
                    "started": start,
                    "duration": None,
                    "viewers": stream.get("viewer_count", 0),
                    "url": f"https://kick.com/{c.get('slug')}",
                    "key": f"kick:{c.get('slug')}:{stream.get('start_time') or today}",
                }
            )
    print(f"[Kick] API returned {returned} of {len(slugs)} channels, {live_now} live right now")
    return results


# ---------------------------------------------------------------- Output ---
def format_line(s, discord=True):
    icon = "🔴 LIVE" if s["status"] == "LIVE" else "⏹️ ENDED"
    game = f" [{s['game']}]" if s["game"] else ""
    views = f"{s['viewers']} viewers" if s["status"] == "LIVE" else f"{s['viewers']} views"

    when = ""
    if s["started"]:
        if discord:
            when = f"Started <t:{int(s['started'].timestamp())}:f>"
        else:
            when = "Started " + s["started"].strftime("%Y-%m-%d %H:%M UTC")
    if s["duration"] is not None:
        when += f" | lasted {fmt_duration(s['duration'])}"

    head = f"{icon} - **{s['platform']}** - {s['name']}{game} ({views})"
    if not discord:
        head = head.replace("**", "")
    return f"{head}\n{s['title']}\n{when}\n{s['url']}".replace("\n\n", "\n")


def post_discord(lines):
    hook = getenv("DISCORD_WEBHOOK_URL")
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
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_seen(seen):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(seen, f)
    except OSError:
        pass


def run_once(cfg, seen, window_hours):
    window_start = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    results = []
    for label, fn, arg in (
        ("Twitch", check_twitch, cfg.get("twitch", [])),
        ("YouTube", check_youtube, cfg.get("youtube", [])),
        ("Kick", check_kick, cfg.get("kick", [])),
    ):
        try:
            results += fn(arg, window_start)
        except Exception as e:  # keep going if one platform fails
            print(f"[{label}] error: {e}", file=sys.stderr)

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    results.sort(
        key=lambda s: (s["status"] != "LIVE", -((s["started"] or epoch).timestamp()))
    )

    new = [s for s in results if s["key"] not in seen]
    now = time.time()
    for s in new:
        seen[s["key"]] = now
    for k in [k for k, t in seen.items() if now - t > SEEN_TTL]:
        del seen[k]
    save_seen(seen)

    print(
        f"\n=== {time.strftime('%H:%M:%S')} | window {window_hours}h | "
        f"{len(results)} found, {len(new)} new ==="
    )
    for s in results:
        print(format_line(s, discord=False) + "\n")

    post_discord([format_line(s) for s in new])


def main():
    global NO_FILTER
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--interval", type=int, default=18000, help="seconds between checks when looping")
    ap.add_argument(
        "--window-hours",
        type=float,
        default=6,
        help="report streams live or ended within this many hours (default 6)",
    )
    ap.add_argument("--no-filter", action="store_true", help="skip the GTA filter (testing)")
    args = ap.parse_args()
    NO_FILTER = args.no_filter

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    seen = load_seen()
    while True:
        run_once(cfg, seen, args.window_hours)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
