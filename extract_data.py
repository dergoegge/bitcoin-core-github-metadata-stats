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

COMMENT_THRESHOLD = 100

# Event types to skip when counting PR activity (non-meaningful noise)
_SKIP_ACTIVITY_EVENTS = frozenset({
    "subscribed", "mentioned", "referenced", "cross-referenced",
    "locked", "unlocked",
})

# Bots to exclude from all extracted data
_IGNORE_USERS = frozenset({"DrahtBot"})

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
        if login in _IGNORE_USERS:
            continue
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
        if login in _IGNORE_USERS:
            continue
        date = c.get("created_at", "")
        if len(date) < 10:
            continue
        keys = period_keys(date)
        for tf, period in keys.items():
            out[tf][period][login] += 1


def _build_contributor_stats(author_period_stats, closed_by_author_period, comment_counts_tf, reviews_received_tf):
    """Build contributor_stats dict including merged/closed PR counts and comment counts."""
    # Collect authors that have at least one merged or closed PR
    all_authors = set(author_period_stats.keys())
    for period_data in closed_by_author_period.values():
        all_authors.update(period_data.keys())

    result = {}
    for author in sorted(
        all_authors,
        key=lambda a: -sum(len(ps["ttm"]) for ps in author_period_stats.get(a, {}).values()),
    ):
        merged_data = author_period_stats.get(author, {})
        # Collect all periods for this author
        author_periods = set(merged_data.keys())
        for period, authors in closed_by_author_period.items():
            if author in authors:
                author_periods.add(period)
        # Also include periods where this author left comments (but only for PR authors)
        for period, users in comment_counts_tf.items():
            if author in users:
                author_periods.add(period)
        # Also include periods where this author received reviews
        for period, users in reviews_received_tf.items():
            if author in users:
                author_periods.add(period)

        author_result = {}
        for p in author_periods:
            pstats = merged_data.get(p)
            closed = closed_by_author_period.get(p, {}).get(author, 0)
            comments = comment_counts_tf.get(p, {}).get(author, 0)
            received = reviews_received_tf.get(p, {}).get(author, 0)
            if pstats:
                author_result[p] = {
                    "count": len(pstats["ttm"]),
                    "closed_count": closed,
                    "comments": comments,
                    "reviews_received": received,
                    "avg_ttm": round(sum(pstats["ttm"]) / len(pstats["ttm"]), 1),
                    "avg_additions": round(sum(pstats["additions"]) / len(pstats["additions"]), 1),
                    "avg_deletions": round(sum(pstats["deletions"]) / len(pstats["deletions"]), 1),
                    "avg_commits": round(sum(pstats["commits"]) / len(pstats["commits"]), 1),
                }
            else:
                author_result[p] = {
                    "count": 0,
                    "closed_count": closed,
                    "comments": comments,
                    "reviews_received": received,
                    "avg_ttm": 0,
                    "avg_additions": 0,
                    "avg_deletions": 0,
                    "avg_commits": 0,
                }
        result[author] = author_result
    return result


def main():
    global USERNAME_MAP

    parser = argparse.ArgumentParser(description="Extract GitHub PR stats into JSON for visualization.")
    parser.add_argument(
        "metadata_repo",
        help="Path to the github-metadata-backup repository",
    )
    parser.add_argument(
        "--username-map",
        help="Path to JSON file mapping old usernames to new ones: {\"old\": \"new\", ...}",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output JSON file path (default: data.json next to this script)",
    )
    args = parser.parse_args()

    backup_dir = args.metadata_repo
    pulls_dir = os.path.join(backup_dir, "pulls")
    issues_dir = os.path.join(backup_dir, "issues")

    if args.username_map:
        with open(args.username_map) as f:
            USERNAME_MAP = json.load(f)
        print(f"Loaded {len(USERNAME_MAP)} username mappings from {args.username_map}", file=sys.stderr)

    # Raw merged PR records: (merge_iso_date, created_iso_date, author, period_keys, additions, deletions, num_commits)
    merged_prs = []
    # Closed-without-merge PR records: (close_iso_date, author, period_keys)
    closed_prs = []
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

    # Per-PR activity data for heatmaps
    pr_activity = {}

    # Label counts: label_counts_pr[timeframe][period][label] = count
    label_counts_pr = {
        "year": defaultdict(lambda: defaultdict(int)),
        "quarter": defaultdict(lambda: defaultdict(int)),
        "month": defaultdict(lambda: defaultdict(int)),
    }
    # Label counts for issues: label_counts_issue[timeframe][period][label] = count
    label_counts_issue = {
        "year": defaultdict(lambda: defaultdict(int)),
        "quarter": defaultdict(lambda: defaultdict(int)),
        "month": defaultdict(lambda: defaultdict(int)),
    }

    # Review clustering: (review_iso_date, pr_age_days) for each review/comment event on a PR
    review_age_events = []

    # Reviews received per PR author: reviews_received[timeframe][period][pr_author] += 1
    reviews_received = {
        "year": defaultdict(lambda: defaultdict(int)),
        "quarter": defaultdict(lambda: defaultdict(int)),
        "month": defaultdict(lambda: defaultdict(int)),
    }

    # --- Read PRs ---
    pr_files = [f for f in os.listdir(pulls_dir) if f.endswith(".json")]
    total_pr = len(pr_files)
    for i, fname in enumerate(pr_files):
        if (i + 1) % 1000 == 0:
            print(f"  Reading PR {i + 1}/{total_pr}...", file=sys.stderr)

        with open(os.path.join(pulls_dir, fname)) as f:
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

        # Collect labels on this PR, bucketed by creation date
        for lbl in pull.get("labels", []):
            label_name = lbl.get("name", "")
            if label_name:
                for tf_key, period in created_keys.items():
                    label_counts_pr[tf_key][period][label_name] += 1

        additions = pull.get("additions", 0) or 0
        deletions = pull.get("deletions", 0) or 0
        num_commits = pull.get("commits", 0) or 0

        merge_event = None
        close_event = None
        for ev in events:
            if ev.get("event") == "merged":
                merge_event = ev
            elif ev.get("event") == "closed" and close_event is None:
                close_event = ev
        if merge_event is not None:
            merge_date = merge_event["created_at"]
            keys = period_keys(merge_date)
            merged_prs.append((merge_date, created_date, author, keys, additions, deletions, num_commits))
            # Track who performed the merge (maintainer with merge access)
            merger = merge_event.get("actor", {}).get("login")
            if merger:
                merge_actors.append((merge_date, map_username(merger)))
        elif close_event is not None:
            close_date = close_event["created_at"]
            keys = period_keys(close_date)
            closed_prs.append((close_date, author, keys))

        # Collect per-PR daily activity for heatmap (per-event-type counts)
        # daily_activity[date][category] = count
        daily_activity = defaultdict(lambda: defaultdict(int))
        _EVENT_CATEGORY = {
            "committed": "commits",
            "commented": "comments",
            "reviewed": "reviews",
            "head_ref_force_pushed": "pushes",
            "merged": "merged",
            "closed": "closed",
            "reopened": "reopened",
        }

        # Also collect engagement metrics
        participants = set()  # non-author users
        first_response_ts = None  # earliest non-author event timestamp
        comments_received = 0  # non-author comments + reviews
        author_updates = 0  # author pushes/commits after creation day
        created_day = created_date[:10]

        for ev in events:
            etype = ev.get("event", "")
            if etype in _SKIP_ACTIVITY_EVENTS:
                continue

            # Resolve event user
            ev_user = None
            if etype == "committed":
                ev_user = (ev.get("author") or {}).get("login")
            else:
                ev_user = (ev.get("user") or ev.get("actor") or {}).get("login")
            if ev_user:
                ev_user = map_username(ev_user)

            # Skip ignored bots entirely for PR stats
            if ev_user in _IGNORE_USERS:
                continue

            date_str = ev.get("created_at") or ""
            if etype == "committed":
                date_str = (ev.get("committer") or {}).get("date") or ""
            if date_str and len(date_str) >= 10:
                cat = _EVENT_CATEGORY.get(etype, "other")
                daily_activity[date_str[:10]][cat] += 1

            # Engagement tracking
            if ev_user and ev_user != author:
                participants.add(ev_user)
                if etype in ("commented", "reviewed"):
                    comments_received += 1
                if date_str and len(date_str) >= 10:
                    if first_response_ts is None or date_str < first_response_ts:
                        first_response_ts = date_str
            elif ev_user == author:
                if etype in ("committed", "head_ref_force_pushed") and date_str[:10] > created_day:
                    author_updates += 1

        for c in comments:
            c_user = (c.get("user") or {}).get("login")
            if c_user:
                c_user = map_username(c_user)
            if c_user in _IGNORE_USERS:
                continue
            date_str = c.get("created_at") or ""
            if date_str and len(date_str) >= 10:
                daily_activity[date_str[:10]]["review comments"] += 1
            if c_user and c_user != author:
                participants.add(c_user)
                comments_received += 1

        # Compute time to first response (days)
        first_response_days = None
        if first_response_ts:
            created_dt = datetime.fromisoformat(created_date.replace("Z", "+00:00"))
            response_dt = datetime.fromisoformat(first_response_ts.replace("Z", "+00:00"))
            first_response_days = round((response_dt - created_dt).total_seconds() / 86400, 1)

        # Compute longest gap between consecutive active days
        active_days = sorted(daily_activity.keys())
        longest_gap = 0
        if len(active_days) >= 2:
            for j in range(1, len(active_days)):
                prev = datetime.fromisoformat(active_days[j - 1])
                curr = datetime.fromisoformat(active_days[j])
                gap = (curr - prev).days
                if gap > longest_gap:
                    longest_gap = gap

        # Collect review age events and reviews-received-by-author for clustering analysis
        created_dt_for_age = datetime.fromisoformat(created_date.replace("Z", "+00:00"))
        for ev in events:
            etype = ev.get("event", "")
            if etype not in ("reviewed", "commented"):
                continue
            ev_user = (ev.get("user") or {}).get("login")
            if ev_user:
                ev_user = map_username(ev_user)
            if ev_user in _IGNORE_USERS or ev_user == author:
                continue
            ts = ev.get("created_at") or ""
            if ts and len(ts) >= 10:
                ev_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_days = (ev_dt - created_dt_for_age).total_seconds() / 86400
                review_age_events.append((ts, age_days))
                keys = period_keys(ts)
                for tf_key, period in keys.items():
                    reviews_received[tf_key][period][author] += 1
        for c in comments:
            c_user = (c.get("user") or {}).get("login")
            if c_user:
                c_user = map_username(c_user)
            if c_user in _IGNORE_USERS or c_user == author:
                continue
            ts = c.get("created_at") or ""
            if ts and len(ts) >= 10:
                ev_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_days = (ev_dt - created_dt_for_age).total_seconds() / 86400
                review_age_events.append((ts, age_days))
                keys = period_keys(ts)
                for tf_key, period in keys.items():
                    reviews_received[tf_key][period][author] += 1

        pr_number = str(pull["number"])
        pr_title = (pull.get("title") or "")[:100]
        pr_closed_at = pull.get("closed_at")
        pr_activity[pr_number] = {
            "title": pr_title,
            "author": author,
            "created": created_date[:10],
            "closed": pr_closed_at[:10] if pr_closed_at else None,
            "merged": merge_event is not None,
            "activity": {date: dict(cats) for date, cats in daily_activity.items()},
            "participants": len(participants),
            "first_response_days": first_response_days,
            "comments_received": comments_received,
            "author_updates": author_updates,
            "longest_gap_days": longest_gap,
        }

    # --- Read issues ---
    if not os.path.isdir(issues_dir):
        issue_files = []
        print(f"  No issues directory found, skipping issues.", file=sys.stderr)
    else:
        issue_files = [f for f in os.listdir(issues_dir) if f.endswith(".json")]
    total_issues = len(issue_files)
    for i, fname in enumerate(issue_files):
        if (i + 1) % 1000 == 0:
            print(f"  Reading issue {i + 1}/{total_issues}...", file=sys.stderr)

        with open(os.path.join(issues_dir, fname)) as f:
            data = json.load(f)

        issue = data.get("issue", data)
        # Collect labels on this issue, bucketed by creation date
        issue_created = issue.get("created_at", "")
        if issue_created and len(issue_created) >= 10:
            issue_keys = period_keys(issue_created)
            for lbl in issue.get("labels", []):
                label_name = lbl.get("name", "")
                if label_name:
                    for tf_key, period in issue_keys.items():
                        label_counts_issue[tf_key][period][label_name] += 1

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
        # Closed (not merged) PRs per author per period
        closed_by_author_period = defaultdict(lambda: defaultdict(int))
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

        # Closed (not merged) PRs per author per period
        for close_date, author, keys in closed_prs:
            closed_by_author_period[keys[tf]][author] += 1

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

        # Review clustering by PR age: bucket review events by PR age at review time
        _AGE_BUCKETS = [
            (0, 7, "<1w"),
            (7, 30, "1-4w"),
            (30, 90, "1-3m"),
            (90, 180, "3-6m"),
            (180, 365, "6-12m"),
            (365, 730, "1-2y"),
            (730, 1e9, "2y+"),
        ]
        _AGE_BUCKET_LABELS = [label for _, _, label in _AGE_BUCKETS]
        review_by_pr_age = defaultdict(lambda: defaultdict(int))  # period -> bucket_label -> count
        for review_date_str, age_days in review_age_events:
            keys = period_keys(review_date_str)
            period = keys[tf]
            for lo, hi, label in _AGE_BUCKETS:
                if lo <= age_days < hi:
                    review_by_pr_age[period][label] += 1
                    break

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
            "contributor_stats": _build_contributor_stats(
                author_period_stats, closed_by_author_period, comment_counts[tf], reviews_received[tf]
            ),
            "review_by_pr_age_buckets": _AGE_BUCKET_LABELS,
            "review_by_pr_age": {
                p: {label: review_by_pr_age.get(p, {}).get(label, 0) for label in _AGE_BUCKET_LABELS}
                for p in all_periods
            },
            "label_counts_pr": {
                p: dict(sorted(label_counts_pr[tf].get(p, {}).items(), key=lambda x: -x[1]))
                for p in all_periods
            },
            "label_counts_issue": {
                p: dict(sorted(label_counts_issue[tf].get(p, {}).items(), key=lambda x: -x[1]))
                for p in all_periods
            },
        }

    output = {
        "comment_threshold": COMMENT_THRESHOLD,
        "timeframes": timeframes,
        "pr_activity": pr_activity,
    }

    if args.output:
        out_path = args.output
    else:
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
