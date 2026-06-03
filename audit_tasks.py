"""Audit and clean the WikiGame task corpus against live Wikipedia.

Validates every ``start`` / ``target`` title in ``tasks.json`` by resolving it
against the live Wikipedia API (following redirects and spelling
normalization). This catches the two ways a randomly generated pair becomes
unwinnable:

  * **Deleted page** — the title no longer exists -> the task is dropped.
  * **Redirect / drifted title** — the stored title is an alias of a different
    canonical title (e.g. ``Wallace (electorate)`` -> ``Division of Wallace``).
    The links the agent sees on pages use the canonical title, so the stored
    title is rewritten to canonical (and the task ``id`` regenerated).

Pairs that collapse to ``start == target`` after canonicalization are dropped.

Scope: this only guarantees that each endpoint *exists and is canonical* — it
does NOT verify the target is reachable from the start within the click budget
(that would need a link-graph crawl and is intentionally out of scope).

Usage:
    python audit_tasks.py            # rewrite tasks.json (backs up to tasks.json.bak)
    python audit_tasks.py --check    # report only, exit non-zero if issues found
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

# Reuse the slug/header/endpoint definitions from the generator — single source
# of truth so audited ids match freshly generated ones.
from generate_tasks import HEADERS, WIKIPEDIA_API, slugify

TASKS_FILE = Path(__file__).parent / "tasks.json"
BACKUP_FILE = Path(__file__).parent / "tasks.json.bak"
BATCH_SIZE = 50  # Wikipedia titles= limit for non-bot clients


def resolve_titles(client: httpx.Client, titles: list[str]) -> dict[str, str | None]:
    """Resolve each title to its canonical Wikipedia title (or None if missing).

    Returns ``{original_title -> canonical_title_or_None}`` for every input.
    """
    result: dict[str, str | None] = {}
    for i in range(0, len(titles), BATCH_SIZE):
        batch = titles[i : i + BATCH_SIZE]
        resp = client.get(
            WIKIPEDIA_API,
            params={
                "action": "query",
                "titles": "|".join(batch),
                "redirects": "1",
                "format": "json",
            },
        )
        resp.raise_for_status()
        query = resp.json().get("query", {})

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
                result[original] = None
            else:
                result[original] = page["title"]
        print(f"  resolved {min(i + BATCH_SIZE, len(titles))}/{len(titles)} titles …")
    return result


def audit(tasks: dict[str, list[dict]], check_only: bool) -> tuple[dict[str, list[dict]], int]:
    """Return (cleaned_tasks, issue_count)."""
    client = httpx.Client(timeout=30.0, headers=HEADERS)
    try:
        unique = sorted({t[k] for split in tasks.values() for t in split for k in ("start", "target")})
        print(f"Resolving {len(unique)} unique titles against live Wikipedia …")
        canonical = resolve_titles(client, unique)
    finally:
        client.close()

    cleaned: dict[str, list[dict]] = {}
    n_missing = n_rewritten = n_collapsed = n_kept = 0

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

            kept.append({"id": f"{slugify(start_c)}_to_{slugify(target_c)}", "start": start_c, "target": target_c})
            n_kept += 1
        cleaned[split] = kept

    total = sum(len(v) for v in tasks.values())
    print(
        f"\nAudit summary: {total} tasks -> {n_kept} kept "
        f"({n_missing} dropped missing, {n_collapsed} dropped collapsed, "
        f"{n_rewritten} rewritten to canonical)."
    )
    print("NOTE: reachability within the click budget is NOT checked — only existence/canonicalization.")
    issues = n_missing + n_collapsed + n_rewritten
    return cleaned, issues


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only; do not write; exit non-zero if issues found")
    args = ap.parse_args()

    tasks = json.loads(TASKS_FILE.read_text())
    cleaned, issues = audit(tasks, check_only=args.check)

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
