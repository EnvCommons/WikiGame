"""Audit and clean the WikiGame task corpus against live Wikipedia.

Validates every ``start`` / ``target`` title in ``tasks.json`` against the live
Wikipedia API and drops or rewrites pairs that a random pairing made unwinnable.

Cheap batched checks (one ``query`` call per 50 titles, always run):
  * **Deleted page** — the title no longer exists -> drop the task.
  * **Redirect / drifted title** — the stored title is an alias of a different
    canonical title (e.g. ``Pay to Stay`` -> ``Pay to stay (housing policy)``).
    The links the agent sees on pages use the canonical title, so the stored
    title is rewritten to canonical (and the task ``id`` regenerated).
  * **Disambiguation target** — the target is a disambiguation page (it exists,
    so it survives the existence check, but no single article ever matches it,
    so it is unwinnable) -> drop the task.
  * **start == target** after canonicalization -> drop the task.

Structural link checks (one call per unique title, run unless --skip-link-checks):
  * **Dead-end start** — the start article has zero outgoing main-namespace
    links, so the agent is stuck on move one -> drop the task.
  * **Orphan target** — the target has zero incoming main-namespace links, so no
    amount of clicking can reach it -> drop the task.

Scope: the link checks catch *provably* unwinnable endpoints (zero out / zero
in). Having links does NOT guarantee the target is reachable within the 20-click
budget — general reachability would need a link-graph crawl and stays out of
scope.

Usage:
    python audit_tasks.py                     # rewrite tasks.json (backs up to tasks.json.bak)
    python audit_tasks.py --check             # report only, exit non-zero if issues found
    python audit_tasks.py --skip-link-checks  # cheap pass only (no per-title link checks)
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

# Reuse the slug/header/endpoint definitions from the generator — single source
# of truth so audited ids match freshly generated ones.
from generate_tasks import HEADERS, WIKIPEDIA_API, slugify

TASKS_FILE = Path(__file__).parent / "tasks.json"
BACKUP_FILE = Path(__file__).parent / "tasks.json.bak"
BATCH_SIZE = 50  # Wikipedia titles= limit for non-bot clients
LINK_CHECK_WORKERS = 8  # concurrency for the per-title link checks


def _get_json(client: httpx.Client, params: dict, label: str) -> dict:
    """GET the Wikipedia API with a few retries; return parsed JSON ({} on failure)."""
    for attempt in range(5):
        try:
            resp = client.get(WIKIPEDIA_API, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001 — audit tool, keep going
            if attempt < 4:
                time.sleep(min(2 ** attempt, 30))
            else:
                print(f"  API ERROR ({label}): {e} — treating as inconclusive")
                return {}
    return {}


def resolve_titles(client: httpx.Client, titles: list[str]) -> tuple[dict[str, str | None], set[str]]:
    """Resolve titles to canonical form and flag disambiguation pages.

    Returns ``({original_title -> canonical_title_or_None}, {canonical disambiguation titles})``.
    """
    canonical: dict[str, str | None] = {}
    disambig: set[str] = set()
    for i in range(0, len(titles), BATCH_SIZE):
        batch = titles[i : i + BATCH_SIZE]
        data = _get_json(
            client,
            {
                "action": "query",
                "titles": "|".join(batch),
                "redirects": "1",
                "prop": "pageprops",
                "ppprop": "disambiguation",
                "format": "json",
            },
            label=f"resolve batch {i // BATCH_SIZE}",
        )
        query = data.get("query", {})
        norm_map = {n["from"]: n["to"] for n in query.get("normalized", [])}
        redir_map = {r["from"]: r["to"] for r in query.get("redirects", [])}
        pages_by_title = {p["title"]: p for p in query.get("pages", {}).values()}

        for original in batch:
            t = norm_map.get(original, original)
            seen: set[str] = set()
            while t in redir_map and t not in seen:
                seen.add(t)
                t = redir_map[t]
            page = pages_by_title.get(t)
            if page is None or "missing" in page or page.get("pageid", 0) < 0:
                canonical[original] = None
            else:
                canonical[original] = page["title"]
                if "disambiguation" in page.get("pageprops", {}):
                    disambig.add(page["title"])
        print(f"  resolved {min(i + BATCH_SIZE, len(titles))}/{len(titles)} titles …")
    return canonical, disambig


def _has_outgoing(client: httpx.Client, title: str) -> bool:
    data = _get_json(
        client,
        {"action": "query", "titles": title, "prop": "links",
         "plnamespace": "0", "pllimit": "1", "format": "json"},
        label=f"outgoing {title}",
    )
    pages = data.get("query", {}).get("pages", {})
    if not pages:
        return True  # inconclusive — don't drop on an API hiccup
    return bool(next(iter(pages.values())).get("links"))


def _has_incoming(client: httpx.Client, title: str) -> bool:
    data = _get_json(
        client,
        {"action": "query", "list": "backlinks", "bltitle": title,
         "blnamespace": "0", "bllimit": "1", "format": "json"},
        label=f"incoming {title}",
    )
    query = data.get("query")
    if query is None:
        return True  # inconclusive — don't drop on an API hiccup
    return bool(query.get("backlinks"))


def _find_bad(client: httpx.Client, titles: set[str], predicate, kind: str) -> set[str]:
    """Return the subset of *titles* for which *predicate* is False (i.e. bad)."""
    titles = sorted(titles)
    bad: set[str] = set()
    done = 0
    with ThreadPoolExecutor(max_workers=LINK_CHECK_WORKERS) as pool:
        for title, ok in zip(titles, pool.map(lambda t: predicate(client, t), titles)):
            if not ok:
                bad.add(title)
            done += 1
            if done % 100 == 0 or done == len(titles):
                print(f"  {kind}: checked {done}/{len(titles)} ({len(bad)} bad) …")
    return bad


def audit(tasks: dict[str, list[dict]], skip_link_checks: bool) -> tuple[dict[str, list[dict]], int]:
    """Return (cleaned_tasks, issue_count)."""
    client = httpx.Client(timeout=30.0, headers=HEADERS)
    try:
        unique = sorted({t[k] for split in tasks.values() for t in split for k in ("start", "target")})
        print(f"Resolving {len(unique)} unique titles against live Wikipedia …")
        canonical, disambig = resolve_titles(client, unique)

        dead_starts: set[str] = set()
        orphan_targets: set[str] = set()
        if not skip_link_checks:
            # Only check endpoints that exist; targets that are disambiguation
            # pages will be dropped anyway, so skip their (pointless) backlink check.
            starts = {canonical[t["start"]] for s in tasks.values() for t in s if canonical.get(t["start"])}
            targets = {canonical[t["target"]] for s in tasks.values() for t in s
                       if canonical.get(t["target"]) and canonical[t["target"]] not in disambig}
            print(f"Checking {len(starts)} start articles for outgoing links …")
            dead_starts = _find_bad(client, starts, _has_outgoing, "outgoing")
            print(f"Checking {len(targets)} target articles for incoming links …")
            orphan_targets = _find_bad(client, targets, _has_incoming, "incoming")
    finally:
        client.close()

    cleaned: dict[str, list[dict]] = {}
    n_missing = n_rewritten = n_collapsed = n_disambig = n_deadstart = n_orphan = n_kept = 0

    for split, pairs in tasks.items():
        kept: list[dict] = []
        for p in pairs:
            start_c = canonical.get(p["start"])
            target_c = canonical.get(p["target"])

            if start_c is None or target_c is None:
                n_missing += 1
                gone = [name for name, c in (("start", start_c), ("target", target_c)) if c is None]
                print(f"  DROP (missing {', '.join(gone)}): {p['id']}")
                continue

            if (start_c, target_c) != (p["start"], p["target"]):
                n_rewritten += 1
                print(f"  REWRITE: {p['start']!r}->{start_c!r}  {p['target']!r}->{target_c!r}")

            if slugify(start_c) == slugify(target_c) or start_c == target_c:
                n_collapsed += 1
                print(f"  DROP (start==target after canonicalization): {p['id']}")
                continue
            if target_c in disambig:
                n_disambig += 1
                print(f"  DROP (disambiguation target {target_c!r}): {p['id']}")
                continue
            if start_c in dead_starts:
                n_deadstart += 1
                print(f"  DROP (dead-end start {start_c!r}, no outgoing links): {p['id']}")
                continue
            if target_c in orphan_targets:
                n_orphan += 1
                print(f"  DROP (orphan target {target_c!r}, no incoming links): {p['id']}")
                continue

            kept.append({"id": f"{slugify(start_c)}_to_{slugify(target_c)}", "start": start_c, "target": target_c})
            n_kept += 1
        cleaned[split] = kept

    total = sum(len(v) for v in tasks.values())
    print(
        f"\nAudit summary: {total} tasks -> {n_kept} kept | "
        f"dropped: {n_missing} missing, {n_collapsed} collapsed, {n_disambig} disambiguation, "
        f"{n_deadstart} dead-end start, {n_orphan} orphan target | {n_rewritten} rewritten to canonical."
    )
    if skip_link_checks:
        print("NOTE: --skip-link-checks: dead-end starts and orphan targets were NOT checked.")
    print("NOTE: link checks catch only zero-out / zero-in endpoints; reachability within the "
          "20-click budget is NOT verified.")
    issues = n_missing + n_collapsed + n_disambig + n_deadstart + n_orphan + n_rewritten
    return cleaned, issues


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only; do not write; exit non-zero if issues found")
    ap.add_argument("--skip-link-checks", action="store_true",
                    help="skip the per-title outgoing/incoming link checks (cheap pass only)")
    args = ap.parse_args()

    tasks = json.loads(TASKS_FILE.read_text())
    cleaned, issues = audit(tasks, skip_link_checks=args.skip_link_checks)

    if args.check:
        if issues:
            print(f"\n--check: {issues} issue(s) found.")
            sys.exit(1)
        print("\n--check: corpus is clean.")
        return

    BACKUP_FILE.write_text(TASKS_FILE.read_text())
    TASKS_FILE.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote cleaned {TASKS_FILE.name} (backup at {BACKUP_FILE.name}).")


if __name__ == "__main__":
    main()
