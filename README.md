# CelesTrak Connector (Fivetran Connector SDK)

English | [日本語](README.ja.md)

A Fivetran Connector SDK connector that syncs satellite data from [CelesTrak](https://celestrak.org). No API key is required.

It syncs three datasets:

| Table | Primary key | Source endpoint | Contents |
|---|---|---|---|
| `gp_element_set` | `norad_cat_id`, `epoch` | `NORAD/elements/gp.php` | GP orbital element sets. One row per satellite per epoch, so history accumulates over time. |
| `gp_group_membership` | `group_name`, `norad_cat_id` | `NORAD/elements/gp.php` | Which configured CelesTrak group each satellite was seen in. |
| `satcat` | `norad_cat_id` | `satcat/records.php` | Satellite catalog (SATCAT): owner, launch/decay dates, orbit type, status. Overwritten with the latest values. |
| `supgp_element_set` | `supgp_file`, `norad_cat_id`, `epoch` | `NORAD/elements/supplemental/sup-gp.php` | Operator-supplied supplemental element sets (for example Starlink, OneWeb). |

Only the primary keys and `norad_cat_id` (`INT`) are declared in the schema. All other columns are inferred, so new CelesTrak fields do not break the sync. Field names are lower-cased (`NORAD_CAT_ID` becomes `norad_cat_id`).

## Configuration

Edit `configuration.json`. All values are strings.

| Key | Default | Description |
|---|---|---|
| `gp_groups` | `stations,active` | Comma-separated CelesTrak GP group names (the `GROUP` parameter). Leave empty to skip GP data. |
| `satcat_groups` | `stations,active` | Comma-separated group names for SATCAT. Leave empty to skip SATCAT. |
| `supgp_files` | `starlink,oneweb` | Comma-separated SupGP file names (the `FILE` parameter). Leave empty to skip SupGP. |
| `min_fetch_interval_hours` | `2` | Minimum hours between fetches of the same query. |

At least one of `gp_groups`, `satcat_groups` or `supgp_files` must be set.

## How it works

- **Full snapshot per query.** CelesTrak has no "changed since" filter, so each query re-reads its current snapshot. Upserts are idempotent.
- **Request frequency limit.** CelesTrak refreshes about every 2 hours and blocks clients that request the same data more often. State stores the last fetch time of each query (for example `gp:stations`), and a query fetched less than `min_fetch_interval_hours` ago is skipped.
- **Checkpointing.** State is checkpointed after each query, so a failure partway through does not repeat completed queries.
- **Retries.** Timeouts, 429 and 5xx responses are retried up to 4 times with exponential backoff (5s, 10s, 20s). A 403 stops the sync immediately with a message, because CelesTrak uses it to block clients that request too often.
- **Empty results.** CelesTrak answers an unknown group or file with plain text such as `No GP data found`. The connector logs a warning and continues.

## Testing

### 1. Set up

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fivetran-connector-sdk
```

`requests` is pre-installed in the Fivetran runtime, so no `requirements.txt` is needed.

### 2. Run the connector locally

From the project directory:

```bash
fivetran debug
```

This runs `connector.py` with `configuration.json` and writes results to a local DuckDB file, `files/warehouse.db`, plus the saved state in `files/state.json`. The first run with the default configuration makes 6 requests (2 GP groups, 2 SATCAT groups, 2 SupGP files). The `active` group is large, so allow a few minutes.

A successful run ends with an operation summary (upserts, checkpoints) and no `SEVERE` lines.

### 3. Inspect the output

```bash
python - <<'EOF'
import duckdb
con = duckdb.connect("files/warehouse.db", read_only=True)
for t in ["gp_element_set", "gp_group_membership", "satcat", "supgp_element_set"]:
    print(t, con.execute(f"select count(*) from tester.{t}").fetchone()[0])
print(con.execute("select norad_cat_id, epoch, object_name from tester.gp_element_set where norad_cat_id = 25544").fetchall())
EOF
```

Tables are created in the `tester` schema. A stations-only run (19 satellites) takes about 15 seconds. Check that:

- Every table has rows (a table is only created once it receives rows, so `supgp_element_set` does not exist if SupGP is disabled or its file names are rejected; the log shows a `No ... data found` warning).
- `satcat` has no `decay_date` column until a decayed object is synced, because columns are inferred from non-null values.
- `epoch` loads as a `TIMESTAMP`. CelesTrak sends it without a timezone; the value is UTC.
- Satellites in more than one group (for example the ISS, 25544, is in both `stations` and `active`) appear once in `gp_element_set` and once per group in `gp_group_membership`.

### 4. Test the repeat-sync behavior

Run `fivetran debug` again right away. Every query should be skipped with `Skipping gp:stations: fetched less than 2.0h ago`, and no HTTP requests are made.

To force a re-fetch while testing, do either of the following (and keep the 2-hour limit in mind; do not do this repeatedly against the real API):

- Set `"min_fetch_interval_hours": "0"` in `configuration.json`.
- Run `fivetran reset` to clear the saved state.

Re-fetching does not create duplicate rows, because upserts use the primary keys above.

### 5. Test with a small configuration first

To keep the first run short and polite to CelesTrak, start with one small group:

```json
{
  "gp_groups": "stations",
  "satcat_groups": "stations",
  "supgp_files": "",
  "min_fetch_interval_hours": "2"
}
```

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| `CelesTrak returned 403 ...` | Too many requests for the same data. Wait a few hours and keep `min_fetch_interval_hours` at 2 or higher. |
| `failed after 4 attempts: HTTP 503` | CelesTrak is temporarily unavailable. Run the sync again later. |
| `CelesTrak returned no data for {...}` warning | The group or file name is not valid. Check the names on the CelesTrak site. |
| `Nothing to sync: set at least one of ...` | All of `gp_groups`, `satcat_groups` and `supgp_files` are empty. |

## Deploying

When local testing passes, deploy with `fivetran deploy`:

```bash
export FIVETRAN_API_KEY=<your base64-encoded API key>
fivetran deploy \
  --api-key "$FIVETRAN_API_KEY" \
  --destination <destination_group_name> \
  --connection <connection_name> \
  --configuration configuration.json \
  --python 3.12
```

- `--destination` is the destination's group name, which can differ from the display name in the dashboard.
- `--connection` becomes the schema name in the destination.
- The deploy creates the connection but does not start the first sync. Start it from the Fivetran dashboard.
- Do not schedule syncs more often than every 2 hours; CelesTrak may block the client with a 403.
- Never commit your API key. `.gitignore` excludes `.env` and `options.toml`.

See the [Connector SDK documentation](https://fivetran.com/docs/connector-sdk) for all options.
