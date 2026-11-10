# -*- coding: utf-8 -*-
"""
Saves a small thumbnail of each post's visual while LinkedIn's link still works.

LinkedIn signs its media links with an expiry date: images die about 20 days
after the scrape, video and carousel slides after about 7. So the dashboard
can't just point at LinkedIn's servers. Instead, each build grabs anything new,
shrinks it, and keeps it in data/media/ for good.

  image     the first image, scaled down
  video     a single still frame (the video itself is never downloaded)
  document  the carousel's cover slide

Uses ffmpeg for all three, which is already on GitHub's runners. If ffmpeg is
missing (a Windows laptop, say) thumbnails are skipped and the build carries on.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

WIDTH = 520           # px; displayed at about 200, so this is sharp on retina
QUALITY = 6           # ffmpeg JPEG scale, 2 best to 31 worst; 6 keeps files ~30-50KB
WORKERS = 6
TIMEOUT = 60          # seconds per item
MAX_ATTEMPTS = 3      # transient failures get retried on later builds, then given up
VIDEO_CAP_MB = 150    # fallback downloads of a whole video stop past this
MANIFEST = "manifest.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def file_id(key):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(key))[-80:]


def expires_at(url):
    """LinkedIn's expiry time from the link's e= parameter, if it has one."""
    try:
        e = parse_qs(urlparse(url).query).get("e")
        return datetime.fromtimestamp(int(e[0]), timezone.utc) if e else None
    except (ValueError, OverflowError, OSError):
        return None


def visual_source(rec):
    """(kind, url) of the visual worth thumbnailing, or (None, None)."""
    if rec.get("video_url"):
        return "video", rec["video_url"]
    doc = rec.get("doc") or {}
    if doc.get("slide_images"):
        return "document", doc["slide_images"][0]
    images = rec.get("images") or []
    if images:
        return "image", images[0]
    return None, None


def _download(url, cap_mb):
    """Whole-file download to a temp file; None if it fails or runs past the cap."""
    fd, path = tempfile.mkstemp(suffix=".bin")
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(path, "wb") as f:
            total = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap_mb * (1 << 20):
                    raise ValueError("over %sMB" % cap_mb)
                f.write(chunk)
        return path
    except Exception:
        os.remove(path)
        return None


def _ffmpeg(url, out, kind):
    scale = "scale='min(%d,iw)':-2" % WIDTH
    base = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
    net = ["-user_agent", UA, "-rw_timeout", str(30 * 1000000)] if url.startswith("http") else []
    tail = ["-frames:v", "1", "-vf", scale, "-q:v", str(QUALITY), str(out)]

    attempts = []
    if kind == "video":
        # A frame a second in usually beats a black opening frame; very short
        # clips fall back to the first frame.
        attempts.append(base + ["-ss", "1"] + net + ["-i", url] + tail)
    attempts.append(base + net + ["-i", url] + tail)

    def run(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, "timed out"
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return True, ""
        lines = (r.stderr or "").strip().splitlines()
        return False, (lines[-1] if lines else "ffmpeg failed")[:160]

    err = ""
    for cmd in attempts:
        ok, err = run(cmd)
        if ok:
            return True, ""

    # Streaming a video can fail when the file keeps its index at the end and
    # the server won't serve byte ranges. Last resort: fetch the file, then cut.
    if kind == "video" and url.startswith("http"):
        local = _download(url, VIDEO_CAP_MB)
        if local:
            try:
                for ss in (["-ss", "1"], []):
                    ok, err = run(base + ss + ["-i", local] + tail)
                    if ok:
                        return True, ""
            finally:
                os.remove(local)

    if out.exists():
        out.unlink()
    return False, err


def update(records, media_dir, key_fn):
    """Fetch thumbnails for anything new, prune ones for posts no longer held.

    Returns {post key: filename} for every thumbnail on disk.
    """
    media_dir = pathlib.Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    mpath = media_dir / MANIFEST
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}

    held = {}
    for rec in records:
        k = key_fn(rec)
        if k:
            held[str(k)] = rec

    # Drop thumbnails for posts that have aged out of the archive.
    pruned = 0
    for k in list(manifest):
        if k not in held:
            f = manifest[k].get("file")
            if f and (media_dir / f).exists():
                (media_dir / f).unlink()
            del manifest[k]
            pruned += 1

    have_ffmpeg = bool(shutil.which("ffmpeg"))
    now = datetime.now(timezone.utc)
    todo, expired = [], 0
    for k, rec in held.items():
        entry = manifest.get(k, {})
        status = entry.get("status")
        if status == "ok" and (media_dir / entry.get("file", "")).is_file():
            continue
        if status == "none":
            continue
        if status == "failed" and entry.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        # "expired" is re-checked every build: the history merge swaps in the
        # newest copy of each post, and a fresh scrape brings a fresh link.
        kind, url = visual_source(rec)
        if not url:
            manifest[k] = {"status": "none"}
            continue
        exp = expires_at(url)
        if exp and exp <= now:
            manifest[k] = {"status": "expired", "kind": kind}
            expired += 1
            continue
        todo.append((k, kind, url, entry.get("attempts", 0)))

    fetched = failed = 0
    if todo and not have_ffmpeg:
        print("media: ffmpeg not found, so %s thumbnails were skipped. They'll be "
              "picked up by the next build that has ffmpeg, if the links are still live." % len(todo))
        todo = []

    def work(item):
        k, kind, url, attempts = item
        name = file_id(k) + ".jpg"
        ok, err = _ffmpeg(url, media_dir / name, kind)
        return k, kind, name, ok, err, attempts

    if todo:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for k, kind, name, ok, err, attempts in pool.map(work, todo):
                if ok:
                    manifest[k] = {"status": "ok", "kind": kind, "file": name}
                    fetched += 1
                else:
                    manifest[k] = {"status": "failed", "kind": kind, "attempts": attempts + 1, "error": err}
                    failed += 1

    mpath.write_text(json.dumps(manifest, indent=0, sort_keys=True), encoding="utf-8")

    ok_files = {k: v["file"] for k, v in manifest.items() if v.get("status") == "ok"}
    print("media: %s thumbnails held (%s new, %s failed, %s links already expired, %s pruned)"
          % (len(ok_files), fetched, failed, expired, pruned))
    return ok_files
