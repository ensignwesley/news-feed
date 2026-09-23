# Command News Feed

A zero-dependency Python collector and read-only localhost HTTP service for Command's digest pipeline. It polls 18 public RSS/Atom feeds, publishes the exact five-field item contract, and retains each source's last-good cache if that source fails.

## Data contract

`data/feed.json` is a JSON array. Every object has exactly:

```json
{"title":"…","url":"…","source":"…","publishedAt":"2026-09-23T16:00:00+00:00","fetchedAt":"2026-09-23T17:00:00+00:00"}
```

Timestamps are ISO 8601 with offsets. `publishedAt` is `null` only when the feed item supplies no date. Output includes at most the newest five eligible items per source, excludes known dates older than two days, and is globally newest-first with null dates last.

`data/status.json` records `generatedAt`, aggregate health, and for each source its configured URL, `lastSuccess`, `lastAttempt`, `itemCount`, and current `error`. A failed source reuses its cached last-good items (still applying the two-day age ceiling) while successful sources advance normally.

Each `--publish-dir` receives exactly `feed.json`, `status.json`, and `health.json`, using atomic file replacement. The public health document contains `healthy`, `generatedAt`, and `failedSources`; its timestamp lets an external probe detect a stopped timer instead of trusting a stale green boolean. Private `cache.json` and `refresh.lock` state is never copied and any stale copies in a dedicated publication directory are removed.

## Run

Requires Python 3.11+ and no third-party packages.

```bash
python3 -m newsfeed.cli refresh \
  --config config/feeds.json \
  --output-dir data \
  --publish-dir /home/jarvis/blog/static/command-news \
  --publish-dir /home/jarvis/blog/public/command-news
python3 -m newsfeed.cli serve --output-dir data --host 127.0.0.1 --port 3011
```

Refresh returns nonzero if any source failed, after atomically publishing retained output and status. Fetches use a 12-second per-attempt timeout and two bounded retries by default. The refresh lock fails immediately if another run owns it. Configuration, output directory, and clock (`--now`) are injectable.

Publication still runs after per-source failures have produced fallback feed/status data; only then does the CLI return nonzero. The first blog path feeds future Hugo builds, while the second updates the current generated document root immediately. This static HTTPS path does not replace the localhost server on port 3011.

Local read-only routes:

- `GET /command-news/feed.json`
- `GET /command-news/status.json`
- `GET /command-news/health`

`HEAD` is supported; POST, PUT, PATCH, and DELETE return 405. The proposed public URLs are `https://wesley.thesisko.com/command-news/...` after the prepared nginx snippet is installed.

## Test

```bash
python3 -m compileall -q newsfeed tests
python3 -m unittest discover -s tests -v
```

The suite includes a deliberate second-run feed failure and proves byte-equivalent item objects survive while status records the new attempt and error. It also proves repeatable publication writes only the three public files, publishes degraded fallback before returning nonzero, and preserves an existing complete file if serialization fails before atomic replacement.

## Prepared deployment (not installed)

- `deploy/systemd/news-feed-refresh.service`
- `deploy/systemd/news-feed-refresh.timer`
- `deploy/systemd/news-feed-server.service`
- `deploy/nginx/command-news.conf`

The user timer uses explicit `Europe/Stockholm` calendar entries at 00:30, 04:30, 08:30, 12:30, 14:35, 16:30, 19:35, and 20:30. Thus the maximum scheduled gap is four hours, and dedicated runs land before all three digest cutoffs. `Persistent=true` catches missed runs after downtime. The prepared refresh service grants write access under `ProtectHome=read-only` only to `data/` and the two dedicated blog publication directories. Deployment is intentionally separate from repository creation and requires operator review.

## Design assumptions

- Feed order in `config/feeds.json` is stable and source names are contract identifiers.
- A syntactically present but unparseable date fails that entire source instead of emitting a misleading `null` date.
- A successfully parsed feed may legitimately publish zero dated items inside the two-day window; undated items remain eligible.
- The health route is red if the latest refresh is older than five hours or any current source has an error. Status/feed routes remain readable during degraded health.
- Atomic replacement is per file. Cache is committed first, then feed and status; public feed, status, and health are then replaced independently. A process interruption is recoverable on the next run without destroying last-good data.
