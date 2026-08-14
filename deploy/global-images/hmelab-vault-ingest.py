#!/usr/bin/env python3
"""Ingest hmelab vault folders into the TDAI Knowledge Wiki.

Usage: python3 hmelab-vault-ingest.py [--wiki NAME] [--no-ingest] FOLDER [FOLDER...]
Folders are relative to /ssd/obsidian/hmelab. Reads team/user ids from
.hmelab-ids and the user key from .hmelab-key in this directory.
Batches uploads at 10 files / <512KB per file / <5MB per call (server limits).
"""
import argparse, json, os, sys, time, urllib.request

BASE = "http://localhost:8424/v3"
VAULT = "/ssd/obsidian/hmelab"
HERE = os.path.dirname(os.path.abspath(__file__))

def api(path, body, user_id):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(),
        {"Content-Type": "application/json", "x-tdai-service-id": "default"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    if d.get("code") not in (0, None):
        raise RuntimeError(f"{path}: {d}")
    return d.get("data", d)


def register_asset(wiki_id, name, team, user):
    """Register the wiki as an llm_wiki asset in Memory Core meta so the
    Panel can resolve it (direct Knowledge-API creates skip this)."""
    key = open(os.path.join(HERE, ".hmelab-key")).read().strip()
    body = {"asset_id": wiki_id, "team_id": team, "asset_type": "llm_wiki",
            "name": name, "owner_user_id": user, "source_type": "manual",
            "visibility": "team"}
    req = urllib.request.Request("http://localhost:8420/v3/meta/asset/create",
        json.dumps(body).encode(),
        {"Content-Type": "application/json", "x-tdai-service-id": "default",
         "x-tdai-user-key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        print(f"asset registered: {d.get('code')}")
    except urllib.error.HTTPError as e:
        print(f"asset register skipped ({e.code}: likely already exists)")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--wiki", default="hmelab-vault")
    ap.add_argument("--no-ingest", action="store_true")
    args = ap.parse_args()

    ids = dict(l.strip().split("=", 1) for l in open(os.path.join(HERE, ".hmelab-ids")) if "=" in l)
    team, user = ids["TEAM_ID"], ids["USER_ID"]

    wiki = api("/wiki/create", {"team_id": team, "user_id": user, "name": args.wiki}, user)
    wiki_id = wiki["wiki_id"]
    print(f"wiki {args.wiki} -> {wiki_id} (status={wiki.get('status')})")
    register_asset(wiki_id, args.wiki, team, user)

    files, skipped = [], 0
    for folder in args.folders:
        root = os.path.join(VAULT, folder)
        for dirpath, _, names in os.walk(root):
            for n in sorted(names):
                if not n.endswith(".md"):
                    continue
                p = os.path.join(dirpath, n)
                rel = os.path.relpath(p, VAULT).replace(os.sep, "__")
                content = open(p, encoding="utf-8", errors="replace").read()
                if len(content.encode()) > 512 * 1024:
                    print(f"  SKIP >512KB: {rel}"); skipped += 1; continue
                files.append({"filename": rel, "content": content})
    print(f"{len(files)} files to upload ({skipped} skipped)")

    batch, size, sent = [], 0, 0
    def flush():
        nonlocal batch, size, sent
        if not batch: return
        api("/wiki/raw/write", {"team_id": team, "user_id": user,
            "wiki_id": wiki_id, "files": batch}, user)
        sent += len(batch)
        print(f"  uploaded {sent}/{len(files)}")
        batch, size = [], 0
    for f in files:
        b = len(f["content"].encode())
        if len(batch) == 10 or size + b > 4_500_000:
            flush()
        batch.append(f); size += b
    flush()

    if not args.no_ingest:
        r = api("/wiki/ingest", {"team_id": team, "user_id": user, "wiki_id": wiki_id}, user)
        print(f"ingest triggered: {r}")
        print("poll with: curl -s localhost:8424/v3/wiki/get -H 'x-tdai-service-id: default' "
              f"-H 'Content-Type: application/json' -d '{{\"wiki_id\":\"{wiki_id}\"}}'")

if __name__ == "__main__":
    main()
