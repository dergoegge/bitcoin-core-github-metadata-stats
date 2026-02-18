#!/usr/bin/env python3
"""Extract PR author stats and comment stats from GitHub metadata backup into a compact JSON for visualization.

Outputs data bucketed by four timeframes: year, quarter (90 days), month, and week.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

BACKUP_DIR = os.path.expanduser("~/workspace/github-metadata-backup-bitcoin-bitcoin")
PULLS_DIR = os.path.join(BACKUP_DIR, "pulls")
ISSUES_DIR = os.path.join(BACKUP_DIR, "issues")

COMMENT_THRESHOLD = 100

# Global username mapping (old_name -> new_name), loaded from CLI arg
USERNAME_MAP = {}


def map_username(login):
    """Map a username through the rename table, following chains."""
    seen = set()
    while login in USERNAME_MAP and login not in seen:
        seen.add(login)
        login = USERNAME_MAP[login]
    return login


def period_keys(iso_date):
    """Return period keys for all timeframes from an ISO 8601 date string."""
    dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    year = dt.year
    month = dt.month
    quarter = (month - 1) // 3 + 1
    return {
        "year": f"{year}",
        "quarter": f"{year}-Q{quarter}",
        "month": f"{year}-{month:02d}",
    }


def collect_comments(events, comments_array, out):
    """Collect comment events and review comments into out[timeframe][period][user] += 1."""
    for ev in events:
        etype = ev.get("event")
        if etype not in ("commented", "reviewed"):
            continue
        user = ev.get("user")
        if user is None:
            continue
        login = map_username(user["login"])
        date = ev.get("created_at") or ev.get("submitted_at", "")
        if len(date) < 10:
            continue
        keys = period_keys(date)
        for tf, period in keys.items():
            out[tf][period][login] += 1

    for c in comments_array:
        user = c.get("user")
        if user is None:
            continue
        login = map_username(user["login"])
        date = c.get("created_at", "")
        if len(date) < 10:
            continue
        keys = period_keys(date)
        for tf, period in keys.items():
            out[tf][period][login] += 1


def main():
    global USERNAME_MAP

    parser = argparse.ArgumentParser(description="Extract GitHub PR stats into JSON for visualization.")
    parser.add_argument(
        "--username-map",
        help="Path to JSON file mapping old usernames to new ones: {\"old\": \"new\", ...}",
    )
    args = parser.parse_args()

    if args.username_map:
        with open(args.username_map) as f:
            USERNAME_MAP = json.load(f)
        print(f"Loaded {len(USERNAME_MAP)} username mappings from {args.username_map}", file=sys.stderr)

    # Raw merged PR records: (merge_iso_date, created_iso_date, author, period_keys, additions, deletions, num_commits)
    merged_prs = []
    # All PR records by creation date: (iso_date, author, period_keys)
    all_prs = []
    # Merge actors: (iso_date, merger_login) — who clicked the merge button
    merge_actors = []

    # comment_counts[timeframe][period][user] = count
    comment_counts = {
        "year": defaultdict(lambda: defaultdict(int)),
        "quarter": defaultdict(lambda: defaultdict(int)),
        "month": defaultdict(lambda: defaultdict(int)),
    }

    # --- Read PRs ---
    pr_files = [f for f in os.listdir(PULLS_DIR) if f.endswith(".json")]
    total_pr = len(pr_files)
    for i, fname in enumerate(pr_files):
        if (i + 1) % 1000 == 0:
            print(f"  Reading PR {i + 1}/{total_pr}...", file=sys.stderr)

        with open(os.path.join(PULLS_DIR, fname)) as f:
            data = json.load(f)

        pull = data["pull"]
        events = data.get("events", [])
        comments = data.get("comments", [])

        collect_comments(events, comments, comment_counts)

        # Track all PR authors by creation date
        author = map_username(pull["user"]["login"])
        created_date = pull["created_at"]
        created_keys = period_keys(created_date)
        all_prs.append((created_date, author, created_keys))

        additions = pull.get("additions", 0) or 0
        deletions = pull.get("deletions", 0) or 0
        num_commits = pull.get("commits", 0) or 0

        merge_event = None
        for ev in events:
            if ev.get("event") == "merged":
                merge_event = ev
                break
        if merge_event is not None:
            merge_date = merge_event["created_at"]
            keys = period_keys(merge_date)
            merged_prs.append((merge_date, created_date, author, keys, additions, deletions, num_commits))
            # Track who performed the merge (maintainer with merge access)
            merger = merge_event.get("actor", {}).get("login")
            if merger:
                merge_actors.append((merge_date, map_username(merger)))

    # --- Read issues ---
    issue_files = [f for f in os.listdir(ISSUES_DIR) if f.endswith(".json")]
    total_issues = len(issue_files)
    for i, fname in enumerate(issue_files):
        if (i + 1) % 1000 == 0:
            print(f"  Reading issue {i + 1}/{total_issues}...", file=sys.stderr)

        with open(os.path.join(ISSUES_DIR, fname)) as f:
            data = json.load(f)

        events = data.get("events", [])
        collect_comments(events, [], comment_counts)

    # --- Compute per-timeframe stats ---
    merged_prs.sort(key=lambda x: x[0])

    # Global set of maintainers (anyone who has ever merged a PR)
    all_maintainers = set(merger for _, merger in merge_actors)

    # First, determine the global first-merge date per author.
    author_first_merge = {}  # author -> iso_date
    for merge_date, created_date, author, keys, additions, deletions, num_commits in merged_prs:
        if author not in author_first_merge:
            author_first_merge[author] = merge_date

    timeframes = {}
    for tf in ("year", "quarter", "month"):
        # Unique merged-PR authors per period
        merged_authors_by_period = defaultdict(set)
        # Merged PR counts per author per period
        prs_by_author_period = defaultdict(lambda: defaultdict(int))
        # Time-to-merge per period (collect individual days for averaging)
        ttm_by_period = defaultdict(list)
        # Also track per-author TTM for filtered averages
        ttm_by_period_with_author = defaultdict(list)  # period -> [(ttm_days, author), ...]
        # TTM by size bucket: period -> { "S": [days], "M": [days], "L": [days] }
        ttm_by_size_period = defaultdict(lambda: {"S": [], "M": [], "L": []})
        # Per-author per-period stats: author -> period -> { ttm: [], additions: [], deletions: [], commits: [] }
        author_period_stats = defaultdict(lambda: defaultdict(lambda: {"ttm": [], "additions": [], "deletions": [], "commits": []}))
        for merge_date, created_date, author, keys, additions, deletions, num_commits in merged_prs:
            merged_authors_by_period[keys[tf]].add(author)
            prs_by_author_period[keys[tf]][author] += 1
            merge_dt = datetime.fromisoformat(merge_date.replace("Z", "+00:00"))
            create_dt = datetime.fromisoformat(created_date.replace("Z", "+00:00"))
            ttm_days = (merge_dt - create_dt).total_seconds() / 86400
            ttm_by_period[keys[tf]].append(ttm_days)
            ttm_by_period_with_author[keys[tf]].append((ttm_days, author))
            # Size bucket: S <= 50 lines, M <= 500 lines, L > 500 lines
            total_lines = additions + deletions
            if total_lines <= 50:
                bucket = "S"
            elif total_lines <= 500:
                bucket = "M"
            else:
                bucket = "L"
            ttm_by_size_period[keys[tf]][bucket].append(ttm_days)
            # Per-author per-period stats
            period = keys[tf]
            author_period_stats[author][period]["ttm"].append(ttm_days)
            author_period_stats[author][period]["additions"].append(additions)
            author_period_stats[author][period]["deletions"].append(deletions)
            author_period_stats[author][period]["commits"].append(num_commits)

        # All PR authors per period (by PR creation date)
        all_authors_by_period = defaultdict(set)
        for created_date, author, keys in all_prs:
            all_authors_by_period[keys[tf]].add(author)

        # Authors without merge per period = opened a PR but had none merged
        no_merge_by_period = {}
        for period in set(all_authors_by_period.keys()) | set(merged_authors_by_period.keys()):
            no_merge_by_period[period] = all_authors_by_period.get(period, set()) - merged_authors_by_period.get(period, set())

        # First-time PR authors per period (based on global first merge)
        first_time_by_period = defaultdict(set)
        for author, first_date in author_first_merge.items():
            first_keys = period_keys(first_date)
            first_time_by_period[first_keys[tf]].add(author)

        # Prolific commenters per period
        prolific_by_period = {}
        for period, user_counts in comment_counts[tf].items():
            prolific = {u: c for u, c in user_counts.items() if c > COMMENT_THRESHOLD}
            prolific_by_period[period] = prolific

        # Collect all periods that have any data
        all_periods = sorted(
            set(merged_authors_by_period.keys())
            | set(all_authors_by_period.keys())
            | set(first_time_by_period.keys())
            | set(prolific_by_period.keys())
        )

        # Merge access: who has performed a merge in each period
        merge_access_by_period = defaultdict(set)
        # Merges by actor: period -> { merger -> count }
        merges_by_actor_period = defaultdict(lambda: defaultdict(int))
        for merge_date, merger in merge_actors:
            keys = period_keys(merge_date)
            merge_access_by_period[keys[tf]].add(merger)
            merges_by_actor_period[keys[tf]][merger] += 1

        # Top 5 authors by total merged PR count across all periods
        global_author_counts = defaultdict(int)
        for p in all_periods:
            for author, count in prs_by_author_period.get(p, {}).items():
                global_author_counts[author] += count
        top5_authors = set(
            a for a, _ in sorted(global_author_counts.items(), key=lambda x: -x[1])[:5]
        )

        timeframes[tf] = {
            "periods": all_periods,
            "unique_author_counts": {p: len(merged_authors_by_period.get(p, set())) for p in all_periods},
            "no_merge_author_counts": {p: len(no_merge_by_period.get(p, set())) for p in all_periods},
            "first_time_author_counts": {p: len(first_time_by_period.get(p, set())) for p in all_periods},
            "prolific_commenter_counts": {p: len(prolific_by_period.get(p, {})) for p in all_periods},
            "merge_access_counts": {p: len(merge_access_by_period.get(p, set())) for p in all_periods},
            "merge_access_users": {p: sorted(merge_access_by_period.get(p, set())) for p in all_periods},
            "unique_authors": {p: sorted(merged_authors_by_period.get(p, set())) for p in all_periods},
            "no_merge_authors": {p: sorted(no_merge_by_period.get(p, set())) for p in all_periods},
            "first_time_authors": {p: sorted(first_time_by_period.get(p, set())) for p in all_periods},
            "prolific_commenter_details": {
                p: dict(sorted(prolific_by_period.get(p, {}).items(), key=lambda x: -x[1]))
                for p in all_periods
            },
            "merges_by_actor": {
                p: dict(sorted(merges_by_actor_period.get(p, {}).items(), key=lambda x: -x[1]))
                for p in all_periods
            },
            "avg_time_to_merge": {
                p: round(sum(ttm_by_period[p]) / len(ttm_by_period[p]), 1) if ttm_by_period.get(p) else 0
                for p in all_periods
            },
            "median_time_to_merge": {
                p: round(sorted(ttm_by_period[p])[len(ttm_by_period[p]) // 2], 1) if ttm_by_period.get(p) else 0
                for p in all_periods
            },
            "prs_by_author": {
                p: dict(sorted(prs_by_author_period.get(p, {}).items(), key=lambda x: -x[1]))
                for p in all_periods
            },
            "avg_time_to_merge_excl_top5": {
                p: (lambda vals: round(sum(vals) / len(vals), 1) if vals else 0)(
                    [ttm for ttm, author in ttm_by_period_with_author.get(p, []) if author not in top5_authors]
                )
                for p in all_periods
            },
            "avg_time_to_merge_excl_maintainers": {
                p: (lambda vals: round(sum(vals) / len(vals), 1) if vals else 0)(
                    [ttm for ttm, author in ttm_by_period_with_author.get(p, []) if author not in all_maintainers]
                )
                for p in all_periods
            },
            "ttm_by_size": {
                bucket: {
                    p: round(sum(ttm_by_size_period[p][bucket]) / len(ttm_by_size_period[p][bucket]), 1)
                    if ttm_by_size_period.get(p, {}).get(bucket) else 0
                    for p in all_periods
                }
                for bucket in ("S", "M", "L")
            },
            "contributor_stats": {
                author: {
                    p: {
                        "count": len(pstats["ttm"]),
                        "avg_ttm": round(sum(pstats["ttm"]) / len(pstats["ttm"]), 1),
                        "avg_additions": round(sum(pstats["additions"]) / len(pstats["additions"]), 1),
                        "avg_deletions": round(sum(pstats["deletions"]) / len(pstats["deletions"]), 1),
                        "avg_commits": round(sum(pstats["commits"]) / len(pstats["commits"]), 1),
                    }
                    for p, pstats in period_data.items()
                }
                for author, period_data in sorted(
                    author_period_stats.items(),
                    key=lambda x: -sum(len(ps["ttm"]) for ps in x[1].values()),
                )
            },
        }

    output = {
        "comment_threshold": COMMENT_THRESHOLD,
        "timeframes": timeframes,
    }

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    with open(out_path, "w") as f:
        json.dump(output, f)

    print(f"Wrote {out_path} ({os.path.getsize(out_path) / 1024 / 1024:.1f} MB)", file=sys.stderr)
    print(f"Total merged PRs: {len(merged_prs)}", file=sys.stderr)
    print(f"Total unique PR authors: {len(author_first_merge)}", file=sys.stderr)
    print(f"Comment threshold: >{COMMENT_THRESHOLD}", file=sys.stderr)
    for tf in ("year", "quarter", "month"):
        print(f"  {tf}: {len(timeframes[tf]['periods'])} periods", file=sys.stderr)


if __name__ == "__main__":
    main()
