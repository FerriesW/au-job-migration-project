"""BigQuery loader and schema definitions for the raw Adzuna landing table."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from google.cloud import bigquery

from .config import get_gcp

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

RAW_TABLE_NAME: Final[str] = "adzuna_jobs"


# --- Schema ----------------------------------------------------------------

ADZUNA_JOBS_SCHEMA: Final[list[bigquery.SchemaField]] = [
    bigquery.SchemaField("snapshot_date", "DATE", mode="REQUIRED",
                         description="Logical partition date for the snapshot."),
    bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="REQUIRED",
                         description="Timestamp the record was uploaded to GCS."),
    bigquery.SchemaField("source_city", "STRING", mode="REQUIRED",
                         description="City filter that produced this record."),

    bigquery.SchemaField("id", "STRING", mode="REQUIRED",
                         description="Adzuna job id."),
    bigquery.SchemaField("title", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("description", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("created", "TIMESTAMP", mode="NULLABLE",
                         description="Job creation timestamp from Adzuna."),
    bigquery.SchemaField("redirect_url", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("adref", "STRING", mode="NULLABLE"),

    bigquery.SchemaField("salary_min", "FLOAT", mode="NULLABLE"),
    bigquery.SchemaField("salary_max", "FLOAT", mode="NULLABLE"),
    bigquery.SchemaField("salary_is_predicted", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("contract_type", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("contract_time", "STRING", mode="NULLABLE"),

    bigquery.SchemaField("latitude", "FLOAT", mode="NULLABLE"),
    bigquery.SchemaField("longitude", "FLOAT", mode="NULLABLE"),

    bigquery.SchemaField(
        "location", "RECORD", mode="NULLABLE",
        fields=[
            bigquery.SchemaField("display_name", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("area", "STRING", mode="REPEATED"),
        ],
    ),
    bigquery.SchemaField(
        "company", "RECORD", mode="NULLABLE",
        fields=[
            bigquery.SchemaField("display_name", "STRING", mode="NULLABLE"),
        ],
    ),
    bigquery.SchemaField(
        "category", "RECORD", mode="NULLABLE",
        fields=[
            bigquery.SchemaField("tag", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("label", "STRING", mode="NULLABLE"),
        ],
    ),
]


# --- Loader ----------------------------------------------------------------

@dataclass(frozen=True)
class LoadResult:
    """Outcome of a BigQuery load job.

    Attributes:
        table_id: Fully qualified destination table.
        rows_loaded: Number of rows persisted by the load job.
        bytes_processed: Byte volume processed by the job.
        job_id: Underlying BigQuery job identifier for traceability.
    """

    table_id: str
    rows_loaded: int
    bytes_processed: int
    job_id: str


class BigQueryRawLoader:
    """Load gzipped JSONL files from GCS into the raw Adzuna table.

    The destination table is created on first call with snapshot_date
    partitioning and (source_city, category.tag) clustering. Subsequent
    loads append to the same table.
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        dataset_raw: str | None = None,
        location: str | None = None,
        table_name: str = RAW_TABLE_NAME,
    ) -> None:
        settings = get_gcp()
        self._project_id: str = project_id or settings.project_id
        self._dataset: str = dataset_raw or settings.dataset_raw
        self._location: str = location or settings.location
        self._table_name: str = table_name
        self._client: bigquery.Client = bigquery.Client(
            project=self._project_id, location=self._location,
        )

    @property
    def table_id(self) -> str:
        """Return the fully qualified destination table identifier."""
        return f"{self._project_id}.{self._dataset}.{self._table_name}"

    def ensure_table(self) -> None:
        """Create the destination table with partitioning and clustering if absent."""
        try:
            self._client.get_table(self.table_id)
            return
        except Exception:
            pass

        table = bigquery.Table(self.table_id, schema=ADZUNA_JOBS_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="snapshot_date",
        )
        table.clustering_fields = ["source_city"]
        table.description = (
            "Raw Adzuna job postings. One row per posting per snapshot. "
            "Partitioned by snapshot_date, clustered by source_city."
        )
        self._client.create_table(table)
        LOGGER.info("Created table %s", self.table_id)

    def load_from_gcs(
        self,
        gcs_uri: str,
        *,
        write_disposition: str = bigquery.WriteDisposition.WRITE_APPEND,
    ) -> LoadResult:
        """Run a load job from a GCS URI into the raw table.

        Args:
            gcs_uri: Source URI in the form ``gs://bucket/key``. Wildcards are
                permitted by BigQuery for multi-file loads.
            write_disposition: BigQuery write disposition; defaults to
                ``WRITE_APPEND`` so successive snapshots accumulate.

        Returns:
            A ``LoadResult`` with the number of rows loaded and job metadata.
        """
        self.ensure_table()

        job_config = bigquery.LoadJobConfig(
            schema=ADZUNA_JOBS_SCHEMA,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=write_disposition,
            ignore_unknown_values=True,
            time_partitioning=bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY,
                field="snapshot_date",
            ),
            clustering_fields=["source_city"],
        )

        load_job = self._client.load_table_from_uri(
            source_uris=gcs_uri,
            destination=self.table_id,
            job_config=job_config,
            location=self._location,
        )
        load_job.result()
        LOGGER.info(
            "Load complete: table=%s rows=%s bytes=%s job=%s",
            self.table_id, load_job.output_rows, load_job.input_file_bytes, load_job.job_id,
        )
        return LoadResult(
            table_id=self.table_id,
            rows_loaded=int(load_job.output_rows or 0),
            bytes_processed=int(load_job.input_file_bytes or 0),
            job_id=load_job.job_id,
        )

    def row_count(self) -> int:
        """Return the current row count of the destination table."""
        query = f"SELECT COUNT(*) AS n FROM `{self.table_id}`"
        result = list(self._client.query(query).result())
        return int(result[0].n)

    def delete_partition(self, snapshot_date: str) -> None:
        """Delete rows for a given snapshot_date partition.

        Useful when re-running an ingestion idempotently and avoiding duplicates
        from a prior failed run on the same date.
        """
        query = (
            f"DELETE FROM `{self.table_id}` "
            f"WHERE snapshot_date = DATE('{snapshot_date}')"
        )
        self._client.query(query).result()
        LOGGER.info("Deleted partition snapshot_date=%s from %s", snapshot_date, self.table_id)
