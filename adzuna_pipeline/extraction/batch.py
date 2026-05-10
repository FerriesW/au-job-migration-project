"""Batch processor: pull pending jobs from BigQuery, extract via Qwen, upsert results."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Final

from google.cloud import bigquery

from ..config import get_dashscope, get_gcp
from .client import ExtractionError, QwenExtractor
from .schema import ExtractionResult, RemoteFriendly, SponsorshipSignal

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

EXTRACT_TABLE_NAME: Final[str] = "adzuna_jobs_llm_extract"
MIN_DESCRIPTION_LENGTH: Final[int] = 20

EXTRACT_SCHEMA: Final[list[bigquery.SchemaField]] = [
    bigquery.SchemaField("job_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("snapshot_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("required_skills", "STRING", mode="REPEATED"),
    bigquery.SchemaField("years_experience", "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("sponsorship_signal", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("local_experience_required", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("remote_friendly", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("model_version", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("extracted_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("extraction_status", "STRING", mode="REQUIRED",
                         description="ok | error"),
    bigquery.SchemaField("error_message", "STRING", mode="NULLABLE"),
]


@dataclass(frozen=True)
class PendingJob:
    """Job awaiting extraction."""

    job_id: str
    snapshot_date: date
    description_text: str


@dataclass
class ExtractionRecord:
    """Result of attempting extraction for one job."""

    job_id: str
    snapshot_date: date
    required_skills: list[str]
    years_experience: int | None
    sponsorship_signal: str
    local_experience_required: bool
    remote_friendly: str
    model_version: str
    extracted_at: datetime
    extraction_status: str
    error_message: str | None

    def to_bq_row(self) -> dict:
        """Serialise to a JSON-friendly dict matching ``EXTRACT_SCHEMA``."""
        return {
            "job_id": self.job_id,
            "snapshot_date": self.snapshot_date.isoformat(),
            "required_skills": self.required_skills,
            "years_experience": self.years_experience,
            "sponsorship_signal": self.sponsorship_signal,
            "local_experience_required": self.local_experience_required,
            "remote_friendly": self.remote_friendly,
            "model_version": self.model_version,
            "extracted_at": self.extracted_at.isoformat(),
            "extraction_status": self.extraction_status,
            "error_message": self.error_message,
        }


class ExtractionBatchProcessor:
    """Drive extraction across many jobs with bounded concurrency.

    Read-side queries select rows from ``stg_adzuna__jobs`` that have no
    matching record in the extract table. Write-side performs a load + MERGE
    so re-running the batch is idempotent on (job_id, snapshot_date).
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        dataset: str | None = None,
        location: str | None = None,
        extractor: QwenExtractor | None = None,
        bigquery_client: bigquery.Client | None = None,
    ) -> None:
        gcp = get_gcp()
        self._project_id: str = project_id or gcp.project_id
        self._dataset: str = dataset or gcp.dataset_staging
        self._location: str = location or gcp.location
        self._client: bigquery.Client = bigquery_client or bigquery.Client(
            project=self._project_id,
            location=self._location,
        )
        self._extractor: QwenExtractor | None = extractor
        self._owns_extractor: bool = extractor is None

    # --- BigQuery operations -------------------------------------------- #

    @property
    def table_id(self) -> str:
        """Fully qualified destination table identifier."""
        return f"{self._project_id}.{self._dataset}.{EXTRACT_TABLE_NAME}"

    @property
    def staging_jobs_table_id(self) -> str:
        """Fully qualified upstream staging table identifier."""
        return f"{self._project_id}.{self._dataset}.stg_adzuna__jobs"

    def ensure_table(self) -> None:
        """Create the destination table on first use with partitioning + clustering."""
        try:
            self._client.get_table(self.table_id)
            return
        except Exception:
            pass

        table = bigquery.Table(self.table_id, schema=EXTRACT_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="snapshot_date",
        )
        table.clustering_fields = ["sponsorship_signal", "remote_friendly"]
        table.description = (
            "Per-job LLM extraction outputs. Upserted on (job_id, snapshot_date) "
            "by the extraction batch processor."
        )
        self._client.create_table(table)
        LOGGER.info("Created table %s", self.table_id)

    def fetch_pending(
        self,
        *,
        snapshot_date: date | None = None,
        sample_size: int = 0,
    ) -> list[PendingJob]:
        """Return staging rows that do not yet have an extraction record.

        Args:
            snapshot_date: Optional partition restriction. If omitted, all
                snapshots that have no extract are considered.
            sample_size: When > 0, cap the number of rows returned. Useful for
                evaluation runs.

        Returns:
            A list of ``PendingJob`` ordered by snapshot_date desc, job_id asc.
        """
        params: list[bigquery.ScalarQueryParameter] = []
        date_clause = ""
        if snapshot_date is not None:
            date_clause = "AND j.snapshot_date = @snapshot_date"
            params.append(
                bigquery.ScalarQueryParameter(
                    "snapshot_date", "DATE", snapshot_date.isoformat(),
                )
            )

        limit_clause = f"LIMIT {int(sample_size)}" if sample_size else ""
        query = f"""
            SELECT
              j.job_id,
              j.snapshot_date,
              j.description_text
            FROM `{self.staging_jobs_table_id}` j
            LEFT JOIN `{self.table_id}` e
              ON e.job_id = j.job_id
             AND e.snapshot_date = j.snapshot_date
             AND e.extraction_status = 'ok'
            WHERE e.job_id IS NULL
              AND j.description_text IS NOT NULL
              AND LENGTH(j.description_text) >= {MIN_DESCRIPTION_LENGTH}
              {date_clause}
            ORDER BY j.snapshot_date DESC, j.job_id
            {limit_clause}
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        rows = self._client.query(query, job_config=job_config).result()
        return [
            PendingJob(
                job_id=row.job_id,
                snapshot_date=row.snapshot_date,
                description_text=row.description_text,
            )
            for row in rows
        ]

    def merge_results(self, records: Sequence[ExtractionRecord]) -> int:
        """Upsert extraction results into the destination table.

        Strategy:
            1. Load the batch into a uniquely-named hidden temp table.
            2. Run MERGE keyed on (job_id, snapshot_date).
            3. Drop the temp table.

        Args:
            records: Records produced by ``run_async``.

        Returns:
            Count of records persisted.
        """
        if not records:
            return 0
        self.ensure_table()

        suffix = int(time.time() * 1000)
        temp_table_id = (
            f"{self._project_id}.{self._dataset}._{EXTRACT_TABLE_NAME}_temp_{suffix}"
        )

        load_config = bigquery.LoadJobConfig(
            schema=EXTRACT_SCHEMA,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        )
        rows = [r.to_bq_row() for r in records]
        load_job = self._client.load_table_from_json(
            rows, temp_table_id, job_config=load_config,
        )
        load_job.result()

        merge_sql = f"""
            MERGE INTO `{self.table_id}` T
            USING `{temp_table_id}` S
            ON T.job_id = S.job_id AND T.snapshot_date = S.snapshot_date
            WHEN MATCHED THEN UPDATE SET
              required_skills = S.required_skills,
              years_experience = S.years_experience,
              sponsorship_signal = S.sponsorship_signal,
              local_experience_required = S.local_experience_required,
              remote_friendly = S.remote_friendly,
              model_version = S.model_version,
              extracted_at = S.extracted_at,
              extraction_status = S.extraction_status,
              error_message = S.error_message
            WHEN NOT MATCHED THEN INSERT ROW
        """
        try:
            self._client.query(merge_sql).result()
        finally:
            self._client.delete_table(temp_table_id, not_found_ok=True)

        return len(records)

    def row_count(self) -> int:
        """Return the row count of the extract table (creates it if missing)."""
        self.ensure_table()
        result = list(self._client.query(
            f"SELECT COUNT(*) AS n FROM `{self.table_id}`"
        ).result())
        return int(result[0].n)

    # --- Async extraction ---------------------------------------------- #

    async def run_async(
        self,
        pending: Sequence[PendingJob],
        *,
        concurrency: int = 5,
    ) -> list[ExtractionRecord]:
        """Extract structured signals for ``pending`` with bounded concurrency.

        Args:
            pending: Jobs to process.
            concurrency: Maximum simultaneous in-flight API calls.

        Returns:
            One ``ExtractionRecord`` per input job, in input order. Records
            with ``extraction_status == "error"`` capture the failure message
            so the caller can persist them and inspect failures later.
        """
        if not pending:
            return []

        sem = asyncio.Semaphore(concurrency)
        extractor = self._extractor or QwenExtractor()
        try:
            tasks = [self._extract_one(job, extractor, sem) for job in pending]
            results = await asyncio.gather(*tasks)
        finally:
            if self._owns_extractor:
                await extractor.aclose()
        return list(results)

    async def _extract_one(
        self,
        job: PendingJob,
        extractor: QwenExtractor,
        sem: asyncio.Semaphore,
    ) -> ExtractionRecord:
        async with sem:
            now = datetime.now(tz=timezone.utc)
            settings = get_dashscope()
            model_version = f"{settings.model}@{now.date().isoformat()}"
            try:
                result = await extractor.extract(job.description_text)
                return self._record_from_result(job, result, model_version, now)
            except ExtractionError as exc:
                LOGGER.warning("Extraction failed for job_id=%s: %s", job.job_id, exc)
                return self._error_record(job, model_version, now, str(exc))
            except Exception as exc:  # noqa: BLE001 - intentional broad capture
                LOGGER.exception("Unexpected error for job_id=%s", job.job_id)
                return self._error_record(job, model_version, now, repr(exc))

    @staticmethod
    def _record_from_result(
        job: PendingJob,
        result: ExtractionResult,
        model_version: str,
        now: datetime,
    ) -> ExtractionRecord:
        return ExtractionRecord(
            job_id=job.job_id,
            snapshot_date=job.snapshot_date,
            required_skills=result.required_skills,
            years_experience=result.years_experience,
            sponsorship_signal=result.sponsorship_signal.value,
            local_experience_required=result.local_experience_required,
            remote_friendly=result.remote_friendly.value,
            model_version=model_version,
            extracted_at=now,
            extraction_status="ok",
            error_message=None,
        )

    @staticmethod
    def _error_record(
        job: PendingJob,
        model_version: str,
        now: datetime,
        message: str,
    ) -> ExtractionRecord:
        return ExtractionRecord(
            job_id=job.job_id,
            snapshot_date=job.snapshot_date,
            required_skills=[],
            years_experience=None,
            sponsorship_signal=SponsorshipSignal.UNSPECIFIED.value,
            local_experience_required=False,
            remote_friendly=RemoteFriendly.UNSPECIFIED.value,
            model_version=model_version,
            extracted_at=now,
            extraction_status="error",
            error_message=message[:500],
        )
