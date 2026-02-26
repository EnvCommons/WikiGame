"""Generate random Wikipedia article pairs for the WikiGame environment.

Fetches random articles from Wikipedia and creates start/target pairs.
Outputs tasks.json with 1000 train pairs and 100 test pairs.
"""

import json
import re
import httpx

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
HEADERS = {
    "User-Agent": "WikiGameEnvironment/1.0 (https://openreward.org; wikigame@openreward.org)",
    "Api-User-Agent": "WikiGameEnvironment/1.0 (https://openreward.org; wikigame@openreward.org)",
    "Accept": "application/json",
}

TRAIN_COUNT = 1000
TEST_COUNT = 100


def slugify(title: str) -> str:
    """Turn an article title into a short ASCII slug for the task id."""
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")[:60]


def fetch_random_titles(client: httpx.Client, count: int) -> list[str]:
    """Fetch *count* unique random main-namespace article titles."""
    titles: set[str] = set()
    while len(titles) < count:
        batch = min(500, count - len(titles))  # API max is 500 per call
        resp = client.get(
            WIKIPEDIA_API,
            params={
                "action": "query",
                "list": "random",
                "rnnamespace": "0",
                "rnlimit": str(batch),
                "format": "json",
            },
        )
        resp.raise_for_status()
        for item in resp.json()["query"]["random"]:
            titles.add(item["title"])
        print(f"  fetched {len(titles)} unique titles so far …")
    return list(titles)


def make_pairs(titles: list[str], count: int) -> list[dict]:
    """Pair up consecutive titles into start/target tasks."""
    pairs = []
    for i in range(count):
        start = titles[2 * i]
        target = titles[2 * i + 1]
        task_id = f"{slugify(start)}_to_{slugify(target)}"
        pairs.append({"id": task_id, "start": start, "target": target})
    return pairs


def main() -> None:
    client = httpx.Client(timeout=30.0, headers=HEADERS)

    # We need 2 * (TRAIN_COUNT + TEST_COUNT) unique articles
    total_needed = 2 * (TRAIN_COUNT + TEST_COUNT)
    print(f"Fetching {total_needed} random Wikipedia articles …")
    all_titles = fetch_random_titles(client, total_needed)

    train_pairs = make_pairs(all_titles, TRAIN_COUNT)
    test_pairs = make_pairs(all_titles[2 * TRAIN_COUNT :], TEST_COUNT)

    # Sanity checks
    for split_name, pairs in [("train", train_pairs), ("test", test_pairs)]:
        for p in pairs:
            assert p["start"] != p["target"], f"Duplicate in {split_name}: {p}"
        print(f"{split_name}: {len(pairs)} pairs OK")

    tasks = {"train": train_pairs, "test": test_pairs}

    with open("tasks.json", "w") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)

    print(f"Wrote tasks.json ({len(train_pairs)} train, {len(test_pairs)} test)")
    client.close()


if __name__ == "__main__":
    main()
