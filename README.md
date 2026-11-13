# outage-tracker

Polls the public APS and SRP outage maps every 5 minutes and builds a record of outage events for metro Phoenix. It uses public, unauthenticated feeds only.

| Utility | Feed |
| --- | --- |
| APS | ArcGIS `APSOutageMap/MapServer` layers 0 (Outages), 5 (SpecialEvents), 8 (PSPS), and 3 (feed timestamp) |
| SRP | `myaccountapi/api/outages/getall` (JSON list) |

## Run

```
pip install requests
python -m outages poll                 # one poll of both utilities
python -m outages loop --minutes 30    # test run, every 5 min
python -m outages summary
python -m outages events --out events.csv
python -m unittest discover tests
```

Set `OUTAGE_CONTACT` to add a contact address to the user agent.

## Unattended runs (GitHub Actions)

The code lives in this public repo, where Actions minutes are free. The data goes to a separate **private** repo, `phoenix-outage-data`. Each run clones it with the `DATA_REPO_TOKEN` secret and pushes its additions back.

- **`poll.yml`** runs every 5 minutes. It calls `log-poll` and appends one line per poll to `log/YYYY/MM/DD/<UTILITY>.jsonl` in the data repo. Each line holds the raw response and whether the poll succeeded. The log is append-only and is the source of truth.
- **`daily.yml`** runs at 01:17 Phoenix time, and also whenever the code changes. It runs the tests, replays the whole log into a fresh event database, and writes `events.csv` and `status.md` to the data repo. If the matching rules change later, all of history is rebuilt with the new rules.
- **Run logs.** The public run logs show only counts, such as "APS: ok; 2 outages", and no outage records.
- **Before the secret exists.** Polls skip with a warning until the `DATA_REPO_TOKEN` secret is set.

**Setup (one time).** Create the private repo `phoenix-outage-data`. Then create a fine-grained token with access to that repo only and Contents set to read/write, and add it to this repo as the Actions secret `DATA_REPO_TOKEN`. When the token expires, polls fail and GitHub emails a failure notice. Renew the token and the polls resume.

## Notes from the first live test (2026-11-13)

- **IDs.** IDs and start times stayed fixed from poll to poll for both utilities.
- **APS times.** APS `off`/`etr` look like Arizona clock time stored as if it were UTC. By default (`APS_TIME_MODE=local`), the scraper shifts them by 7 hours. The raw values are kept in each observation, so the choice can be undone.
- **SRP times.** SRP times are true UTC.
- **Planned work.** Planned work gets `kind = planned`, from APS `outagetype` or SRP `isMaintenanceOutage`.
- **APS coverage.** The APS feed covers the whole state. `in_metro` is a rough box flag. The final clip to the study area uses the Census urban-area polygon.
- **Location.** Both feeds give one point per outage plus street limits as text (APS `Boundary`, SRP `crossRoadText`). Neither gives a polygon.

## New outage or the same one? (`outages/tracker.py`)

Each poll is matched against the events we already know, separately for each utility:

1. **Continuing**: the outage has the same utility and outage ID as an open event (APS `Ticket`, SRP `outageId`).
2. **Reopened**: the ID matches an event that closed less than 30 minutes ago. It stays the same event, and `n_reopen` goes up by one.
3. **ID changed**: the ID is new, but the reported start time is within 2 minutes and the location is within 250 m of a known event. The new ID is kept, and the old one goes into `id_aliases`.
4. **New**: anything that doesn't match by rules 1 to 3.
5. **Closed**: an open event that is missing from a *successful* poll. Restoration happened between `end_lo` (the last poll that saw it) and `end_hi` (this poll).

A failed poll changes nothing, so missing data is never counted as a restoration. Every poll is logged in `polls` with `ok = 0/1`, including empty ones. This is how the data distinguishes "no outages" from "scraper down".

## Storage

- `data/raw/<UTILITY>/YYYY/MM/DD/HHMMSS.json.gz`: each raw response, verbatim, so events can be rebuilt with new rules.
- `data/outages.sqlite`: three tables.
  - `polls`: one row per request.
  - `observations`: one row per outage per poll, with the action taken and which fields changed.
  - `events`: one row per outage, with start, end bounds, peak customers, cause and location.
