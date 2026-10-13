#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build the LinkedIn trending-topics dashboard.

Pull the latest run straight from Apify and write a dated dashboard:

    export APIFY_TOKEN=apify_api_xxxxxxxx
    python build_dashboard.py --task my-linkedin-task --client "Client name"

Or rebuild from an export you already have on disk:

    python build_dashboard.py --file dataset_linkedin-posts-scraper-pro.json

Add --history data/history.json to merge each pull into an accumulating archive
before building. The scraper caps posts per profile, so a single run gives very
uneven history per voice; merging run after run fixes that over time.

The token is read from the environment and never written into the output, so
the HTML you send a client contains post data and nothing else.

To run the actor itself rather than read its last run, use --run-actor with the
actor id and pass the input JSON with --actor-input.
"""
import argparse
import json
import os
import pathlib
import sys
from datetime import date

from prepare import prepare

HERE = pathlib.Path(__file__).resolve().parent
API = "https://api.apify.com/v2"


def fetch(path, params):
    """GET from the Apify API, turning HTTP errors into readable messages."""
    import urllib.error
    import urllib.parse
    import urllib.request

    url = API + path + "?" + urllib.parse.urlencode(params)
    shown = url.replace(params.get("token") or "\x00", "<token>")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        hint = {
            401: "The token is wrong, or it has been revoked.",
            403: "The token is valid but lacks permission for this resource.",
            404: "Apify has nothing at that id. Check whether it is a task or an "
                 "actor, and that it has at least one SUCCEEDED run.\n"
                 "Run with --list-tasks to print the ids on this account.",
        }.get(e.code, "")
        sys.exit("\nApify returned %s for:\n  %s\n\n%s\n\n%s" % (e.code, shown, body, hint))
    except urllib.error.URLError as e:
        sys.exit("Could not reach the Apify API: %s" % e.reason)


def token():
    t = os.environ.get("APIFY_TOKEN")
    if not t:
        sys.exit("APIFY_TOKEN is not set. Export it first, or pass --file to build from a local export.")
    return t


def list_tasks():
    """Print the tasks and recent actor runs on this account, with their ids."""
    tok = token()
    tasks = fetch("/actor-tasks", {"token": tok, "limit": 100}).get("data", {}).get("items", [])
    print("\nTASKS  (use the id with --task)")
    if not tasks:
        print("  none on this account")
    for t in tasks:
        print("  %-20s %s" % (t.get("id"), t.get("name") or "?"))

    runs = fetch("/actor-runs", {"token": tok, "limit": 10, "desc": "true"}).get("data", {}).get("items", [])
    print("\nRECENT ACTOR RUNS  (use the dataset id with --dataset)")
    for r in runs:
        print("  %-10s started %s   dataset %s" % (
            r.get("status"), (r.get("startedAt") or "")[:16], r.get("defaultDatasetId")))
    print()


def from_task(task_id):
    """Items from the last successful run of a saved task."""
    return fetch(
        "/actor-tasks/%s/runs/last/dataset/items" % task_id,
        {"token": token(), "status": "SUCCEEDED", "clean": "true", "format": "json"},
    )


def from_dataset(dataset_id):
    return fetch(
        "/datasets/%s/items" % dataset_id,
        {"token": token(), "clean": "true", "format": "json"},
    )


def run_actor(actor_id, input_path):
    """Run the actor now and wait for it, then read its dataset."""
    import urllib.parse
    import urllib.request

    payload = json.load(open(input_path)) if input_path else {}
    url = "%s/acts/%s/run-sync-get-dataset-items?%s" % (
        API,
        actor_id.replace("/", "~"),
        urllib.parse.urlencode({"token": token(), "clean": "true", "format": "json"}),
    )
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    # run-sync has a five minute ceiling; for longer scrapes use a schedule
    # plus --task instead of running the actor inline.
    with urllib.request.urlopen(req, timeout=330) as r:
        return json.loads(r.read().decode("utf-8"))


def post_key(record):
    meta = record.get("_metadata") or {}
    return meta.get("post_id") or record.get("post_url") or record.get("share_url")


def merge_history(raw, path, max_age_days=None):
    """Fold this pull into an accumulating archive, keyed on post id.

    Where a post is already held, the newer copy wins: engagement counts keep
    climbing after publication, so the later collection is the better figure.
    """
    store = {}
    p = pathlib.Path(path)
    if p.exists():
        for rec in json.loads(p.read_text(encoding="utf-8")):
            k = post_key(rec)
            if k:
                store[k] = rec

    before = len(store)
    added = updated = 0
    for rec in raw:
        k = post_key(rec)
        if not k:
            continue
        old = store.get(k)
        if old is None:
            store[k] = rec
            added += 1
        else:
            new_at = ((rec.get("_metadata") or {}).get("extracted_at")) or ""
            old_at = ((old.get("_metadata") or {}).get("extracted_at")) or ""
            if new_at >= old_at:
                store[k] = rec
                updated += 1

    records = sorted(store.values(), key=lambda r: r.get("posted_at") or "", reverse=True)

    if max_age_days:
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
        kept = [r for r in records if (r.get("posted_at") or "")[:10] >= cutoff]
        dropped = len(records) - len(kept)
        records = kept
        if dropped:
            print("history: dropped %s posts older than %s days" % (dropped, max_age_days))

    parent = p.parent
    if parent.exists() and not parent.is_dir():
        sys.exit(
            "Cannot write the history: '%s' already exists as a file, not a folder.\n"
            "Delete it from the repository and let the build create the folder, or "
            "point --history somewhere else." % parent
        )
    parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.is_dir():
        sys.exit("--history points at a folder, not a file: %s" % p)
    p.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    print("history: %s held (%s new, %s refreshed, was %s)" % (len(records), added, updated, before))
    return records


def build(raw, client, title, out_dir):
    data = prepare(raw)
    if not data["posts"]:
        sys.exit("No usable posts in that dataset. Check the export has content and _metadata.source_url.")

    template = (HERE / "template.html").read_text(encoding="utf-8")
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")

    html = (
        template.replace("__DATA__", blob)
        .replace("__CLIENT__", client)
        .replace("__TITLE__", title)
        .replace("__W_COMMENTS__", str(data["weights"]["comments"]))
        .replace("__W_SHARES__", str(data["weights"]["shares"]))
    )

    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() else "-" for c in client.lower()).strip("-") or "dashboard"
    out = out_dir / ("%s-linkedin-signal-%s.html" % (slug, date.today().isoformat()))
    out.write_text(html, encoding="utf-8")
    # index.html so the same file serves as a hosted site root
    (out_dir / "index.html").write_text(html, encoding="utf-8")

    print("%s posts, %s tracked voices" % (len(data["posts"]), len(data["authors"])))
    print("written: %s (%.0f KB)" % (out, out.stat().st_size / 1024))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--task", help="Apify task id — reads its last successful run")
    src.add_argument("--dataset", help="Apify dataset id")
    src.add_argument("--run-actor", help="actor id to run now, e.g. user~linkedin-posts-scraper-pro")
    src.add_argument("--file", help="local Apify export (JSON array)")
    src.add_argument("--list-tasks", action="store_true",
                     help="print the task and dataset ids on this account, then exit")
    ap.add_argument("--actor-input", help="JSON file of actor input, used with --run-actor")
    ap.add_argument("--history", help="JSON archive to merge this pull into, then build from")
    ap.add_argument("--max-age-days", type=int, help="drop archived posts older than this")
    ap.add_argument("--client", default="Client", help="name shown above the title")
    ap.add_argument("--title", default="LinkedIn signal board", help="dashboard title")
    ap.add_argument("--out", default="out", help="output directory")
    a = ap.parse_args()

    if a.list_tasks:
        list_tasks()
        return

    for name, value in (("--task", a.task), ("--dataset", a.dataset), ("--run-actor", a.run_actor)):
        if value is not None and not value.strip():
            sys.exit("%s was passed but empty. In GitHub Actions that means the matching "
                     "repository variable is missing or misspelled \u2014 check the Variables "
                     "tab, not Secrets." % name)

    if a.file:
        raw = json.load(open(a.file, encoding="utf-8"))
    elif a.task:
        raw = from_task(a.task.strip())
    elif a.dataset:
        raw = from_dataset(a.dataset.strip())
    else:
        raw = run_actor(a.run_actor, a.actor_input)

    if isinstance(raw, dict):
        raw = raw.get("items", [])
    print("pulled %s records" % len(raw))

    if a.history:
        raw = merge_history(raw, a.history, a.max_age_days)

    build(raw, a.client, a.title, a.out)


if __name__ == "__main__":
    main()
