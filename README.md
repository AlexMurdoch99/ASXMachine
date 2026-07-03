# ASXMachine

Tracks which ASX tickers are being discussed in a small set of Australian investing subreddits, for my own private investment research.

> **Status: pre-approval.** This tool will not be run against the live Reddit API until Data API access has been granted under Reddit's [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy).

## What it does

1. Once daily (via cron), fetches the public **hot** and **new** listings (~150 posts each) from four subreddits: r/ASX_Bets, r/ausstocks, r/ASX, r/AusFinance.
2. Parses post titles and selftext for ASX ticker symbols — `$`-prefixed and bare 3–5 letter codes — validated against the official ASX listed-companies directory to eliminate false positives.
3. Stores aggregate statistics in a local SQLite database: ticker, subreddit, post score, comment count, timestamps, keyed by post ID for deduplication.
4. Prints a local summary report: top tickers over the last 24 hours with an upvote/comment tally and a momentum ratio against the prior 6-day baseline.

## What it never does

- No posts, comments, votes, messages, or moderation — **read-only**, application-only OAuth with no write scopes.
- No post content, titles, or usernames are retained. Raw text is discarded after ticker extraction.
- No AI/ML training, no user profiling, no redistribution or resale of any Reddit data. Output stays on my machine.
- Estimated API usage: ~16 requests per day in a single brief cycle — a rounding error against free-tier limits.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env   # then fill in credentials
```

1. Register a **script**-type app at reddit.com/prefs/apps (redirect URI `http://localhost:8080`) and put the client ID/secret in `.env`.
2. Download the ASX listed-companies directory CSV from asx.com.au and save it as `asx_companies.csv` (or point `ASX_LIST_PATH` at it).
3. Wait for Reddit Data API approval before running any scans.

Suggested `.gitignore` (keep credentials and data local):

```
.env
*.db
asx_companies.csv
__pycache__/
```

## Usage

```bash
python asx_tracker.py scan      # one polling cycle
python asx_tracker.py report    # top tickers, last 24h vs baseline
python asx_tracker.py           # both
```

Cron example (daily at 6pm Brisbane time, after ASX close):

```
0 18 * * * cd /path/to/ASXMachine && .venv/bin/python asx_tracker.py scan >> scan.log 2>&1
```

## Data stored

Single table `mentions(post_id, ticker, subreddit, post_score, num_comments, post_created_utc, first_seen_utc, last_seen_utc)` — engagement counts only, nothing reproducing Reddit content.

## Licence / scope

Personal, non-commercial project. Not affiliated with Reddit or the ASX. Not investment advice — it counts posts, it doesn't pick stocks.
