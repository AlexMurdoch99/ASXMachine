#!/usr/bin/env python3
"""
ASXMachine — read-only ASX ticker mention tracker.

Polls the public 'hot' and 'new' listings of a small set of Australian
investing subreddits, extracts ASX ticker symbols from post titles and
selftext, and stores aggregate mention statistics in a local SQLite
database for personal investment research.

This script is strictly read-only. It never submits posts or comments,
never votes, never messages, and retains no post content, titles, or
usernames — only ticker symbols and per-post engagement counts keyed by
post ID for deduplication.

NOTE: Do not run this against the live Reddit API until Data API access
has been approved under Reddit's Responsible Builder Policy.

Usage:
    python asx_tracker.py scan      # one polling cycle (intended for cron)
    python asx_tracker.py report    # print top tickers, last 24h vs 7d baseline
    python asx_tracker.py           # scan, then report

Intended cadence: one scan per day (via cron), ideally after ASX close so
the day's discussion has settled. Estimated API usage per scan: ~16
requests (4 subs x 2 listings x ~2 pages at 100 posts/page), negligible
against free-tier limits.
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import praw
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Configuration (override via .env)
# --------------------------------------------------------------------------
CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USERNAME = os.getenv("REDDIT_USERNAME", "")  # used only in the User-Agent
SUBREDDITS = [
    s.strip()
    for s in os.getenv("SUBREDDITS", "ASX_Bets,ausstocks,ASX,AusFinance").split(",")
    if s.strip()
]
POLL_LIMIT = int(os.getenv("POLL_LIMIT", "150"))
DB_PATH = os.getenv("DB_PATH", "asxmachine.db")
ASX_LIST_PATH = os.getenv("ASX_LIST_PATH", "asx_companies.csv")

USER_AGENT = f"python:asxmachine:v1.0 (by /u/{REDDIT_USERNAME or 'unknown'})"

# Tickers matched with a $ prefix ($PLS) always count if they're real ASX
# codes. Bare uppercase matches (PLS) also count, unless the code doubles
# as a common English word that produces false positives in caps-heavy
# subreddit prose. Tune to taste.
AMBIGUOUS_BARE = {"ALL", "CAN", "FOR", "NEW", "ONE", "AIR", "EAT", "TIP", "GEM", "BID"}

DOLLAR_RE = re.compile(r"\$([A-Za-z]{3,5})\b")
BARE_RE = re.compile(r"\b([A-Z]{3,5})\b")


# --------------------------------------------------------------------------
# ASX code whitelist
# --------------------------------------------------------------------------
def load_asx_codes(path: str) -> set[str]:
    """
    Load valid ASX codes from the official listed-companies CSV
    (asx.com.au directory export). Tolerant of preamble lines and
    varying column names: uses the first column whose header contains
    'code', falling back to the second column.
    """
    p = Path(path)
    if not p.exists():
        sys.exit(
            f"ASX company list not found at '{path}'.\n"
            "Download the listed-companies directory CSV from asx.com.au "
            "and save it there (or set ASX_LIST_PATH in .env)."
        )

    codes: set[str] = set()
    with p.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    header_idx, code_col = None, None
    for i, row in enumerate(rows):
        lowered = [c.strip().lower() for c in row]
        for j, cell in enumerate(lowered):
            if "code" in cell:
                header_idx, code_col = i, j
                break
        if header_idx is not None:
            break
    if header_idx is None:
        header_idx, code_col = 0, 1  # fallback: assume second column

    for row in rows[header_idx + 1 :]:
        if len(row) > code_col:
            code = row[code_col].strip().upper()
            if 3 <= len(code) <= 5 and code.isalpha():
                codes.add(code)

    if not codes:
        sys.exit(f"Parsed zero ASX codes from '{path}' — check the file format.")
    return codes


def extract_tickers(text: str, valid: set[str]) -> set[str]:
    """Return the set of validated ASX codes mentioned in text."""
    found: set[str] = set()
    for m in DOLLAR_RE.findall(text):
        code = m.upper()
        if code in valid:
            found.add(code)
    for code in BARE_RE.findall(text):
        if code in valid and code not in AMBIGUOUS_BARE:
            found.add(code)
    return found


# --------------------------------------------------------------------------
# Storage — no content, no usernames; post_id kept solely for dedup
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS mentions (
    post_id          TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    subreddit        TEXT NOT NULL,
    post_score       INTEGER,
    num_comments     INTEGER,
    post_created_utc INTEGER,
    first_seen_utc   INTEGER,
    last_seen_utc    INTEGER,
    PRIMARY KEY (post_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_mentions_created ON mentions (post_created_utc);
CREATE INDEX IF NOT EXISTS idx_mentions_ticker  ON mentions (ticker);
"""

UPSERT = """
INSERT INTO mentions
    (post_id, ticker, subreddit, post_score, num_comments,
     post_created_utc, first_seen_utc, last_seen_utc)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (post_id, ticker) DO UPDATE SET
    post_score   = excluded.post_score,
    num_comments = excluded.num_comments,
    last_seen_utc = excluded.last_seen_utc;
"""


def db_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------
def make_reddit() -> praw.Reddit:
    if not (CLIENT_ID and CLIENT_SECRET):
        sys.exit(
            "Missing REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET in .env.\n"
            "Reminder: do not run scans until Reddit has approved Data API "
            "access for this app under the Responsible Builder Policy."
        )
    # Application-only OAuth: read-only access to public listings.
    return praw.Reddit(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        user_agent=USER_AGENT,
    )


def scan(conn: sqlite3.Connection, valid: set[str]) -> None:
    reddit = make_reddit()
    reddit.read_only = True
    now = int(time.time())
    rows = 0

    for sub_name in SUBREDDITS:
        sub = reddit.subreddit(sub_name)
        for listing in (sub.hot(limit=POLL_LIMIT), sub.new(limit=POLL_LIMIT)):
            for post in listing:
                text = f"{post.title}\n{post.selftext or ''}"
                for ticker in extract_tickers(text, valid):
                    conn.execute(
                        UPSERT,
                        (
                            post.id,
                            ticker,
                            sub_name,
                            post.score,
                            post.num_comments,
                            int(post.created_utc),
                            now,
                            now,
                        ),
                    )
                    rows += 1
    conn.commit()
    print(f"[scan] {time.strftime('%Y-%m-%d %H:%M:%S')} upserted {rows} ticker-post rows")


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def report(conn: sqlite3.Connection, hours: int = 24, top: int = 20) -> None:
    now = int(time.time())
    recent_cutoff = now - hours * 3600
    baseline_cutoff = now - 7 * 24 * 3600

    q_recent = """
        SELECT ticker,
               COUNT(DISTINCT post_id) AS posts,
               SUM(post_score)         AS score,
               SUM(num_comments)       AS comments
        FROM mentions
        WHERE post_created_utc >= ?
        GROUP BY ticker
    """
    q_baseline = """
        SELECT ticker, COUNT(DISTINCT post_id) AS posts
        FROM mentions
        WHERE post_created_utc >= ? AND post_created_utc < ?
        GROUP BY ticker
    """

    recent = {r[0]: r for r in conn.execute(q_recent, (recent_cutoff,))}
    baseline = dict(conn.execute(q_baseline, (baseline_cutoff, recent_cutoff)).fetchall())

    def momentum(ticker: str, posts: int) -> float:
        prior_daily = baseline.get(ticker, 0) / 6.0  # ~6 prior days
        return posts / prior_daily if prior_daily else float(posts)

    ranked = sorted(
        recent.values(),
        key=lambda r: (r[1], r[2] or 0),
        reverse=True,
    )[:top]

    print(f"\nTop tickers — last {hours}h (baseline: prior 6 days)")
    print(f"{'TICKER':<8}{'POSTS':>6}{'SCORE':>8}{'CMNTS':>7}{'MOMENTUM':>10}")
    for ticker, posts, score, comments in ranked:
        print(
            f"{ticker:<8}{posts:>6}{(score or 0):>8}{(comments or 0):>7}"
            f"{momentum(ticker, posts):>9.1f}x"
        )
    if not ranked:
        print("(no data yet — run some scans first)")


# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="ASXMachine — ASX ticker mention tracker")
    parser.add_argument("command", nargs="?", default="all", choices=["scan", "report", "all"])
    parser.add_argument("--hours", type=int, default=24, help="report window (default 24)")
    args = parser.parse_args()

    valid = load_asx_codes(ASX_LIST_PATH)
    conn = db_connect(DB_PATH)

    if args.command in ("scan", "all"):
        scan(conn, valid)
    if args.command in ("report", "all"):
        report(conn, hours=args.hours)
    conn.close()


if __name__ == "__main__":
    main()
