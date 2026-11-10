"""This connector syncs satellite data from CelesTrak (https://celestrak.org): GP orbital element sets,
the SATCAT satellite catalog, and SupGP supplemental element sets. No API key is required.
See the Technical Reference documentation (https://fivetran.com/docs/connector-sdk/technical-reference)
and the Best Practices documentation (https://fivetran.com/docs/connector-sdk#connectordevelopmentbestpractices) for details
"""

# For reading configuration from a JSON file
import json

# For exponential backoff between API retries
import time

# For recording when each CelesTrak query was last fetched
from datetime import datetime, timezone

# For making HTTP requests to the CelesTrak API
import requests

# Import required classes from fivetran_connector_sdk
from fivetran_connector_sdk import Connector

# For enabling Logs in your connector code
from fivetran_connector_sdk import Logging as log

# For supporting Data operations like upsert(), update(), delete() and checkpoint()
from fivetran_connector_sdk import Operations as op

__BASE_URL = "https://celestrak.org"
__GP_URL = f"{__BASE_URL}/NORAD/elements/gp.php"
__SATCAT_URL = f"{__BASE_URL}/satcat/records.php"
__SUPGP_URL = f"{__BASE_URL}/NORAD/elements/supplemental/sup-gp.php"

__USER_AGENT = "fivetran-celestrak-connector/1.0"
__REQUEST_TIMEOUT_SECONDS = 120
__MAX_RETRIES = 4
__BACKOFF_BASE_SECONDS = 5
__RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# CelesTrak updates most data about every 2 hours and blocks clients that request the same
# data more often, so a query is skipped if it was fetched more recently than this many hours ago.
__DEFAULT_MIN_FETCH_INTERVAL_HOURS = 2.0

# Prefix used in state keys; one key per CelesTrak query, e.g. "gp:stations".
__GP = "gp"
__SATCAT = "satcat"
__SUPGP = "supgp"


def parse_list(value):
    """
    Split a comma-separated configuration value into a list of non-empty, trimmed items.
    Args:
        value: the raw configuration string, or None.
    Returns:
        A list of strings. An empty or missing value gives an empty list, which disables that dataset.
    """
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def validate_configuration(configuration: dict):
    """
    Validate the configuration dictionary.
    All values are optional; this only checks that the ones provided are usable.
    Args:
        configuration: a dictionary that holds the configuration settings for the connector.
    Raises:
        ValueError: if min_fetch_interval_hours is not a non-negative number, or no dataset is enabled.
    """
    interval = configuration.get("min_fetch_interval_hours")
    if interval not in (None, ""):
        try:
            if float(interval) < 0:
                raise ValueError
        except ValueError:
            raise ValueError("min_fetch_interval_hours must be a non-negative number")

    if not any(
        parse_list(configuration.get(key)) for key in ("gp_groups", "satcat_groups", "supgp_files")
    ):
        raise ValueError(
            "Nothing to sync: set at least one of gp_groups, satcat_groups or supgp_files"
        )


def schema(configuration: dict):
    """
    Define the schema function which lets you configure the schema your connector delivers.
    See the technical reference documentation for more details on the schema function:
    https://fivetran.com/docs/connector-sdk/technical-reference/connector-sdk-methods#schema
    Only keys and the NORAD id type are declared; remaining columns are inferred so the
    schema can evolve if CelesTrak adds fields.
    Args:
        configuration: a dictionary that holds the configuration settings for the connector.
    """
    return [
        {
            "table": "gp_element_set",  # One row per satellite per element set epoch (history is kept).
            "primary_key": ["norad_cat_id", "epoch"],
            "columns": {"norad_cat_id": "INT"},
        },
        {
            "table": "gp_group_membership",  # Which configured CelesTrak group each satellite was seen in.
            "primary_key": ["group_name", "norad_cat_id"],
            "columns": {"norad_cat_id": "INT"},
        },
        {
            "table": "satcat",  # One row per catalogued object; overwritten with its latest status.
            "primary_key": ["norad_cat_id"],
            "columns": {"norad_cat_id": "INT"},
        },
        {
            "table": "supgp_element_set",  # Operator-supplied element sets, one row per file/satellite/epoch.
            "primary_key": ["supgp_file", "norad_cat_id", "epoch"],
            "columns": {"norad_cat_id": "INT"},
        },
    ]


def fetch_json(url: str, params: dict):
    """
    Fetch a CelesTrak query and return the parsed JSON list.
    Transient failures (timeouts, 429 and 5xx responses) are retried with exponential backoff.
    A 403 is not retried because CelesTrak uses it to block clients that request data too often.
    Args:
        url: the CelesTrak endpoint.
        params: query string parameters.
    Returns:
        A list of record dictionaries, empty if CelesTrak reports no matching data.
    Raises:
        RuntimeError: if the request keeps failing or the response is not usable JSON.
    """
    last_error = None
    for attempt in range(1, __MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers={"User-Agent": __USER_AGENT},
                timeout=__REQUEST_TIMEOUT_SECONDS,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = f"{type(e).__name__}: {e}"
        else:
            if response.status_code == 403:
                raise RuntimeError(
                    f"CelesTrak returned 403 for {params}; the client is probably blocked for requesting "
                    "the same data too often. Increase min_fetch_interval_hours or sync less often."
                )
            if response.status_code not in __RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return parse_json_body(response, params)
            last_error = f"HTTP {response.status_code}"

        if attempt < __MAX_RETRIES:
            delay = __BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning(f"Request for {params} failed ({last_error}); retrying in {delay}s")
            time.sleep(delay)

    raise RuntimeError(f"CelesTrak request for {params} failed after {__MAX_RETRIES} attempts: {last_error}")


def parse_json_body(response, params: dict):
    """
    Parse a CelesTrak response body as a JSON list.
    CelesTrak answers an empty query with a plain-text message (e.g. "No GP data found") instead of JSON.
    Args:
        response: the requests response.
        params: the query parameters, used for error messages.
    Returns:
        A list of record dictionaries.
    """
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        if text.lower().startswith("no "):
            log.warning(f"CelesTrak returned no data for {params}: {text[:100]}")
            return []
        raise RuntimeError(f"CelesTrak returned a non-JSON response for {params}: {text[:200]}")
    if not isinstance(body, list):
        raise RuntimeError(f"Expected a JSON list from CelesTrak for {params}, got {type(body).__name__}")
    return body


def normalize(record: dict):
    """
    Lower-case the field names of a CelesTrak record (e.g. NORAD_CAT_ID -> norad_cat_id).
    """
    return {key.lower(): value for key, value in record.items()}


def is_due(state: dict, state_key: str, min_interval_hours: float):
    """
    Decide whether a CelesTrak query should be fetched now, based on when it was last fetched.
    Args:
        state: the connector state.
        state_key: the key identifying the query, e.g. "gp:stations".
        min_interval_hours: minimum hours between fetches of the same query.
    """
    last = state.get("last_fetched", {}).get(state_key)
    if not last:
        return True
    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last)
    return elapsed.total_seconds() >= min_interval_hours * 3600


def mark_fetched(state: dict, state_key: str):
    """
    Record in state that a query has just been fetched and persist it.
    Checkpointing after each query means a failure later in the sync does not repeat completed queries.
    """
    state.setdefault("last_fetched", {})[state_key] = datetime.now(timezone.utc).isoformat()
    # Checkpoint saves the state so the next sync resumes from here, and tells Fivetran it is
    # safe to write the rows upserted so far to the destination.
    op.checkpoint(state)


def sync_gp_group(group: str):
    """
    Upsert the current GP element sets for one CelesTrak group, plus group membership rows.
    Returns:
        The number of element sets written.
    """
    records = fetch_json(__GP_URL, {"GROUP": group, "FORMAT": "json"})
    count = 0
    for raw in records:
        record = normalize(raw)
        if record.get("norad_cat_id") is None or record.get("epoch") is None:
            log.warning(f"Skipping GP record without norad_cat_id/epoch in group {group}")
            continue
        # The 'upsert' operation inserts a new row or updates the row with the same primary key.
        op.upsert(table="gp_element_set", data=record)
        op.upsert(
            table="gp_group_membership",
            data={"group_name": group, "norad_cat_id": record["norad_cat_id"]},
        )
        count += 1
    return count


def sync_satcat_group(group: str):
    """
    Upsert SATCAT catalog records for one CelesTrak group.
    Returns:
        The number of catalog records written.
    """
    records = fetch_json(__SATCAT_URL, {"GROUP": group, "FORMAT": "json"})
    count = 0
    for raw in records:
        record = normalize(raw)
        if record.get("norad_cat_id") is None:
            log.warning(f"Skipping SATCAT record without norad_cat_id in group {group}")
            continue
        op.upsert(table="satcat", data=record)
        count += 1
    return count


def sync_supgp_file(supgp_file: str):
    """
    Upsert SupGP supplemental element sets for one supplemental file (e.g. "starlink").
    Returns:
        The number of element sets written.
    """
    records = fetch_json(__SUPGP_URL, {"FILE": supgp_file, "FORMAT": "json"})
    count = 0
    for raw in records:
        record = normalize(raw)
        if record.get("norad_cat_id") is None or record.get("epoch") is None:
            log.warning(f"Skipping SupGP record without norad_cat_id/epoch in file {supgp_file}")
            continue
        record["supgp_file"] = supgp_file
        op.upsert(table="supgp_element_set", data=record)
        count += 1
    return count


def update(configuration: dict, state: dict):
    """
    Define the update function, which is a required function, and is called by Fivetran during each sync.
    See the technical reference documentation for more details on the update function
    https://fivetran.com/docs/connector-sdk/technical-reference/connector-sdk-methods#update
    CelesTrak has no "changed since" filter, so each due query re-reads its full current snapshot.
    State only remembers when each query was last fetched, to respect CelesTrak's request frequency limit.
    Args:
        configuration: A dictionary containing connection details
        state: A dictionary containing state information from previous runs
        The state dictionary is empty for the first sync or for any full re-sync
    """
    validate_configuration(configuration=configuration)

    interval_value = configuration.get("min_fetch_interval_hours")
    min_interval_hours = (
        float(interval_value) if interval_value not in (None, "") else __DEFAULT_MIN_FETCH_INTERVAL_HOURS
    )

    jobs = (
        [(__GP, group, sync_gp_group) for group in parse_list(configuration.get("gp_groups"))]
        + [(__SATCAT, group, sync_satcat_group) for group in parse_list(configuration.get("satcat_groups"))]
        + [(__SUPGP, name, sync_supgp_file) for name in parse_list(configuration.get("supgp_files"))]
    )

    for dataset, name, sync_fn in jobs:
        state_key = f"{dataset}:{name}"
        if not is_due(state, state_key, min_interval_hours):
            log.info(f"Skipping {state_key}: fetched less than {min_interval_hours}h ago")
            continue

        log.info(f"Syncing {state_key}")
        count = sync_fn(name)
        log.info(f"Synced {count} record(s) for {state_key}")
        mark_fetched(state, state_key)

    # Checkpoint once more so a sync where every query was skipped still completes cleanly.
    op.checkpoint(state)


# Create the connector object using the schema and update functions
connector = Connector(update=update, schema=schema)

# Check if the script is being run as the main module.
# This is Python's standard entry method allowing your script to be run directly from the command line or IDE 'run' button.
#
# IMPORTANT: The recommended way to test your connector is using the Fivetran debug command:
#   fivetran debug
#
# This local testing block is provided as a convenience for quick debugging during development,
# such as using IDE debug tools (breakpoints, step-through debugging, etc.).
# Note: This method is not called by Fivetran when executing your connector in production.
# Always test using 'fivetran debug' prior to finalizing and deploying your connector.
if __name__ == "__main__":
    # Open the configuration.json file and load its contents
    with open("configuration.json", "r") as f:
        configuration = json.load(f)

    # Test the connector locally
    connector.debug(configuration=configuration)
