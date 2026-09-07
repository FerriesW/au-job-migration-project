"""One interface over the three query engines this project runs on.

The multi-cloud claim in ADR-0002 is that BigQuery, Athena and Snowflake give
the same answers over the same data. Checking that means asking each engine a
question and comparing the replies — but the three clients agree on almost
nothing: BigQuery returns typed Python objects from a blocking call, Athena
wants a submit-poll-fetch cycle and hands back every cell as a string with a
header row attached, Snowflake needs a session and returns `Decimal` where the
others return `int`.

`QueryEngine` hides all of that behind one method. Callers name a dialect and
get rows back; every value arrives as a string, so results from different
engines can be compared directly rather than case-by-case. That normalisation
is the point of the module — without it, "do the engines agree?" turns into an
argument about `4704` versus `'4704'` versus `Decimal('4704')`.

The interface is deliberately three things: a name, `run`, and `close`.
Everything else — credentials, polling, header rows, type coercion — sits
behind it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Protocol

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

POLL_SECONDS: Final[float] = 1.0
POLL_TIMEOUT_SECONDS: Final[float] = 180.0
ATHENA_TERMINAL_STATES: Final[frozenset[str]] = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


def normalise(value: Any) -> str | None:
    """Render one cell as a string that means the same thing on every engine.

    Athena reports every column as text, BigQuery returns native Python types,
    and Snowflake returns `Decimal` for aggregates. Booleans are the trap:
    Python's `str(True)` is `'True'` while Athena sends `'true'`, so they are
    lower-cased explicitly rather than left to `str`.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        # Aggregates come back as Decimal('4704'); normalise to the integer
        # form the other two engines produce.
        return str(int(value)) if value == value.to_integral_value() else str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


@dataclass(frozen=True)
class QueryResult:
    """Rows plus whatever the engine was willing to say about the cost.

    Attributes:
        rows: Result rows, header excluded, every cell normalised to a string.
        scanned_bytes: Bytes the engine reports scanning, or None where it does
            not expose the figure per query.
        elapsed_ms: Wall-clock execution time as reported by the engine, or
            None where it is not available.
    """

    rows: list[tuple[str | None, ...]]
    scanned_bytes: int | None
    elapsed_ms: int | None


class QueryEngine(Protocol):
    """A warehouse that can answer a SQL question.

    Implementations differ in dialect — that is the whole subject of this
    project — so the SQL is the caller's problem. Everything about *reaching*
    the engine is not.
    """

    @property
    def name(self) -> str:
        """Short identifier used in reports, e.g. `bigquery`."""
        ...

    def run(self, sql: str) -> QueryResult:
        """Execute ``sql`` and return its rows, blocking until it finishes."""
        ...

    def close(self) -> None:
        """Release any session the engine holds. Safe to call more than once."""
        ...


class BigQueryEngine:
    """BigQuery, via the google-cloud-bigquery client."""

    def __init__(self, *, project_id: str | None = None, location: str | None = None) -> None:
        from google.cloud import bigquery  # noqa: PLC0415

        from .config import get_gcp  # noqa: PLC0415

        gcp = get_gcp()
        self._project = project_id or gcp.project_id
        self._client = bigquery.Client(
            project=self._project,
            location=location or gcp.location,
        )

    @property
    def name(self) -> str:
        return "bigquery"

    def run(self, sql: str) -> QueryResult:
        from google.cloud import bigquery  # noqa: PLC0415

        # The result cache is disabled deliberately. BigQuery serves a repeated
        # identical query from cache and reports zero bytes processed, which
        # silently turns a cost comparison into a measurement of how recently
        # the same question was asked. Athena and Snowflake are not being asked
        # to serve from cache here either, so this keeps the three comparable.
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(use_query_cache=False),
        )
        rows = [tuple(normalise(v) for v in row.values()) for row in job.result()]
        return QueryResult(
            rows=rows,
            scanned_bytes=job.total_bytes_processed,
            elapsed_ms=_millis_between(job.started, job.ended),
        )

    def close(self) -> None:
        self._client.close()  # type: ignore[no-untyped-call]


class AthenaEngine:
    """Athena, which is Trino, reached through boto3.

    Athena has no blocking execute: a query is submitted, polled until it
    reaches a terminal state, then fetched. The first row of the result set is
    the header and is dropped here so callers never see it.
    """

    def __init__(self, *, region: str | None = None, workgroup: str | None = None) -> None:
        import boto3  # noqa: PLC0415

        from .config import get_aws  # noqa: PLC0415

        aws = get_aws()
        if not aws.athena_output:
            raise ValueError("ATHENA_OUTPUT_S3 is not set; Athena needs a results location")
        self._output = aws.athena_output
        self._workgroup = workgroup or aws.athena_workgroup
        self._client = boto3.client("athena", region_name=region or aws.region)

    @property
    def name(self) -> str:
        return "athena"

    def run(self, sql: str) -> QueryResult:
        execution_id = self._client.start_query_execution(
            QueryString=sql,
            WorkGroup=self._workgroup,
            ResultConfiguration={"OutputLocation": self._output},
        )["QueryExecutionId"]

        execution = self._await(execution_id)
        state = execution["Status"]["State"]
        if state != "SUCCEEDED":
            raise RuntimeError(
                f"Athena query {state}: {execution['Status'].get('StateChangeReason', '')}"
            )

        result = self._client.get_query_results(QueryExecutionId=execution_id)
        raw = result["ResultSet"]["Rows"][1:]  # first row is the header
        rows = [tuple(cell.get("VarCharValue") for cell in row["Data"]) for row in raw]
        stats = execution.get("Statistics", {})
        return QueryResult(
            rows=rows,
            scanned_bytes=int(stats.get("DataScannedInBytes", 0)),
            elapsed_ms=int(stats.get("TotalExecutionTimeInMillis", 0)),
        )

    def _await(self, execution_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while True:
            execution = self._client.get_query_execution(QueryExecutionId=execution_id)[
                "QueryExecution"
            ]
            if execution["Status"]["State"] in ATHENA_TERMINAL_STATES:
                return dict(execution)
            if time.monotonic() > deadline:
                self._client.stop_query_execution(QueryExecutionId=execution_id)
                raise TimeoutError(f"Athena query exceeded {POLL_TIMEOUT_SECONDS:.0f}s")
            time.sleep(POLL_SECONDS)

    def close(self) -> None:
        return None


class SnowflakeEngine:
    """Snowflake, over a session held open across queries.

    Logging in costs a second or two, so the connection is created once on
    first use and reused. Snowflake does not expose per-query credit usage
    without ACCOUNTADMIN, so `scanned_bytes` is reported instead where
    available and left as None otherwise.
    """

    def __init__(self) -> None:
        from .config import get_snowflake  # noqa: PLC0415

        self._settings = get_snowflake()
        self._connection: Any | None = None

    @property
    def name(self) -> str:
        return "snowflake"

    def _connect(self) -> Any:
        if self._connection is None:
            import snowflake.connector as connector  # noqa: PLC0415

            s = self._settings
            self._connection = connector.connect(
                account=s.account,
                user=s.user,
                password=s.password,
                role=s.role,
                warehouse=s.warehouse,
                database=s.database,
                login_timeout=30,
            )
        return self._connection

    def run(self, sql: str) -> QueryResult:
        cursor = self._connect().cursor()
        try:
            cursor.execute(sql)
            rows = [tuple(normalise(v) for v in row) for row in cursor.fetchall()]
            # Snowflake bills credits, not bytes, and per-query credit usage is
            # only readable from ACCOUNT_USAGE with ACCOUNTADMIN. Reporting a
            # zero here would read as "scanned nothing", so it stays None.
            return QueryResult(rows=rows, scanned_bytes=None, elapsed_ms=None)
        finally:
            cursor.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def _millis_between(started: datetime | None, ended: datetime | None) -> int | None:
    if started is None or ended is None:
        return None
    return int((ended - started).total_seconds() * 1000)
