# Access-log report tool

Write a command-line tool `report.py` that summarizes a web access log.

## Usage

```
python3 report.py <logfile> <command> [args]
```

## Log format

Each line of the log has six space-separated fields:

```
<timestamp> <method> <path> <status> <bytes> <ms>
```

for example:

```
2026-02-01T09:00:03 GET /products 200 3400 55
```

`timestamp` is an ISO-8601 instant (no spaces), `status` is the HTTP status
code, `bytes` is the response size, and `ms` is the response time in
milliseconds. Blank lines are ignored.

## Commands

Each command prints to standard output:

- **`top-paths <N>`** — the `N` most-requested paths, one per line as
  `<path> <count>`, ordered by count descending. Break ties on count by path
  name ascending.
- **`status-counts`** — every status code that appears and how many times, one
  per line as `<status> <count>`, ordered by status code ascending.
- **`error-rate`** — the percentage of requests whose status is `400` or
  greater, printed to one decimal place with a trailing percent sign, e.g.
  `25.0%`.
- **`bytes-total`** — the sum of the `bytes` field over all requests, as a
  single integer.
- **`slowest <N>`** — the `N` requests with the highest `ms`, one per line as
  `<path> <ms>`, ordered by `ms` descending. Break ties on `ms` by path name
  ascending.

## Example

Given a log whose requests are five `/home`, five `/products`, and three
`/search` (plus others):

```
$ python3 report.py access.log top-paths 3
/home 5
/products 5
/search 3
```

Done when each command produces exactly the output described above.
