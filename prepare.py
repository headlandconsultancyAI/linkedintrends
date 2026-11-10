# -*- coding: utf-8 -*-
"""
Turns a raw Apify LinkedIn-posts dataset into the compact JSON the dashboard
reads. Shared by build_dashboard.py; no network calls happen in here.
"""
import re
import collections
from datetime import datetime, timezone

import entities as ents_mod
from topics import TOPICS

# Engagement weighting. Comments and shares cost the reader more effort than a
# reaction, so they are worth more as a signal of a topic landing.
W_REACTIONS = 1
W_COMMENTS = 3
W_SHARES = 5

EXCERPT_CHARS = 550


def _compile(terms):
    pats = []
    for t in terms:
        alts = [re.escape(a.strip()) for a in t.split("|")]
        pats.append(r"(?<![\w'])(?:" + "|".join(alts) + r")(?![\w'])")
    return re.compile("|".join(pats), re.IGNORECASE)


TOPIC_PATTERNS = {label: _compile(terms) for label, terms in TOPICS.items()}


def engagement(post):
    e = post.get("engagement") or {}
    return (
        W_REACTIONS * (e.get("total_reactions") or 0)
        + W_COMMENTS * (e.get("comments") or 0)
        + W_SHARES * (e.get("shares") or 0)
    )


def tracked_authors(raw):
    """Map each monitored profile URL to the name that profile posts under.

    Needed because on a repost `author_name` is the *original* poster, not the
    profile being tracked. The source_url in _metadata is the reliable key.
    """
    votes = collections.defaultdict(collections.Counter)
    for p in raw:
        url = (p.get("_metadata") or {}).get("source_url")
        if not url:
            continue
        if p.get("is_repost"):
            name = (p.get("reposted_by") or {}).get("name")
        else:
            name = p.get("author_name")
        if name:
            votes[url][name] += 1
    return {url: c.most_common(1)[0][0] for url, c in votes.items()}


def tag(text):
    """Return (topic labels, the words that actually triggered them)."""
    hits = []
    for label, pat in TOPIC_PATTERNS.items():
        found = pat.findall(text)
        if found:
            hits.append((label, len(found), found))
    # Strongest signals first, capped so one post can't colour every topic.
    hits.sort(key=lambda x: -x[1])
    hits = hits[:4]
    labels, terms = [], []
    for label, _, found in hits:
        seen = []
        for f in found:
            f = f.lower().strip()
            if f and f not in seen:
                seen.append(f)
        labels.append(label)
        terms.append(seen[:6])
    return labels, terms


def media_kind(post):
    if post.get("video_url"):
        return "video"
    if post.get("doc"):
        return "document"
    if post.get("article"):
        return "article"
    if post.get("images"):
        return "image"
    return "text"


def _entity_text(post):
    art = post.get("article") or {}
    doc = post.get("doc") or {}
    return " ".join(
        [post.get("content") or "", art.get("title") or "", doc.get("title") or ""]
    )


def _mention_types(raw):
    out = {}
    for p in raw:
        for m in p.get("mentions") or []:
            name = (m.get("name") or "").strip()
            if name:
                out[name.lower()] = "person" if m.get("type") == "PROFILE_MENTION" else "org"
    return out


def extract_entities(raw, profiles):
    """Per-post named entities, plus a registry of the ones worth showing.

    Everything found is kept, including subjects named in a single post, so the
    tags under each post are complete. The board's voice threshold is what
    keeps one-off name-drops and extraction noise out of the overlap view.
    """
    low, up = ents_mod.casing_stats([_entity_text(p) for p in raw])
    mention_types = _mention_types(raw)

    per_post, counts = [], collections.Counter()
    for p in raw:
        found = {}
        for form in ents_mod.extract(_entity_text(p), low, up):
            found.setdefault(form.lower(), form)
        for m in p.get("mentions") or []:
            name = (m.get("name") or "").strip()
            if name:
                found.setdefault(name.lower(), name)
        for h in re.findall(r"#(\w{3,})", p.get("content") or ""):
            if h.lower() not in ents_mod.HASHTAG_BLOCK:
                found.setdefault("#" + h.lower(), "#" + h)
        for k in [k for k in found if ents_mod.excluded(k)]:
            del found[k]
        per_post.append(found)
        counts.update(found.keys())

    alias = ents_mod.fold_aliases(counts)
    alias = {k: v for k, v in alias.items() if not ents_mod.excluded(v)}

    merged = []
    totals, forms = collections.Counter(), collections.defaultdict(collections.Counter)
    for found in per_post:
        keys = set()
        for k, form in found.items():
            canon = alias.get(k, k)
            keys.add(canon)
            forms[canon][form] += 1
        merged.append(keys)
        totals.update(keys)

    keep = {k for k, n in totals.items() if n >= 1}
    registry, index = [], {}
    for k in sorted(keep, key=lambda k: (-totals[k], k)):
        # Prefer the surface form that matches the canonical key, so a folded
        # alias doesn't relabel "Dolly Parton" as "Dolly".
        exact = [f for f in forms[k] if f.lower() == k]
        name = exact[0] if exact else forms[k].most_common(1)[0][0]
        index[k] = len(registry)
        registry.append({"name": name, "kind": ents_mod.classify(name, mention_types)})

    ids = [sorted(index[k] for k in keys if k in index) for keys in merged]
    return registry, ids


def post_key(record):
    meta = record.get("_metadata") or {}
    return meta.get("post_id") or record.get("post_url") or record.get("share_url")


def prepare(raw, thumbs=None, media_prefix="media/"):
    """thumbs maps post key -> saved thumbnail filename (from media.update)."""
    thumbs = thumbs or {}
    profiles = tracked_authors(raw)
    registry, ent_ids = extract_entities(raw, profiles)
    posts = []

    for pos, p in enumerate(raw):
        meta = p.get("_metadata") or {}
        url = meta.get("source_url")
        author = profiles.get(url)
        if not author:
            continue

        content = (p.get("content") or "").strip()
        article = p.get("article") or {}
        doc = p.get("doc") or {}
        # Link posts and document posts often carry the substance in the
        # attachment title rather than the post body.
        haystack = " ".join(
            [content, article.get("title") or "", article.get("description") or "", doc.get("title") or ""]
        )

        if not haystack.strip():
            continue

        excerpt = re.sub(r"\s+", " ", content)
        truncated = len(excerpt) > EXCERPT_CHARS
        if truncated:
            excerpt = excerpt[:EXCERPT_CHARS].rsplit(" ", 1)[0] + "\u2026"

        labels, terms = tag(haystack)
        e = p.get("engagement") or {}
        posts.append(
            {
                "author": author,
                "profile": url,
                "date": (p.get("posted_at") or "")[:10],
                "url": p.get("post_url"),
                "excerpt": excerpt,
                "reactions": e.get("total_reactions") or 0,
                "comments": e.get("comments") or 0,
                "shares": e.get("shares") or 0,
                "score": engagement(p),
                "topics": labels,
                "terms": terms,
                "ents": ent_ids[pos],
                "media": media_kind(p),
                "thumb": (media_prefix + thumbs[str(post_key(p))]) if str(post_key(p)) in thumbs else None,
                "n_img": len(p.get("images") or []) if not p.get("video_url") else 0,
                "n_slides": len((p.get("doc") or {}).get("slide_images") or []),
                "art": ((p.get("article") or {}).get("title") or None) if media_kind(p) == "article" else None,
                "repost": bool(p.get("is_repost")),
                "reposted_from": p.get("author_name") if p.get("is_repost") else None,
                "mentions": sorted({m.get("name") for m in (p.get("mentions") or []) if m.get("name")}),
            }
        )

    posts.sort(key=lambda x: x["date"], reverse=True)

    coverage = {}
    for a in sorted(profiles.values()):
        dates = sorted(p["date"] for p in posts if p["author"] == a)
        if dates:
            coverage[a] = {"posts": len(dates), "from": dates[0], "to": dates[-1]}

    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "authors": sorted(coverage),
        "coverage": coverage,
        "topic_labels": list(TOPICS),
        "entities": registry,
        "weights": {"reactions": W_REACTIONS, "comments": W_COMMENTS, "shares": W_SHARES},
        "posts": posts,
    }
