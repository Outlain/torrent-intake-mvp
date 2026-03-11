from __future__ import annotations
from datetime import datetime, timedelta
import logging
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import get_settings
from .models import Job
from .qbt import QbtService, TorrentAlreadyExistsError
from .scanner import ScannerService
from .telegram import TelegramService


class JobService:
    TERMINAL_STATES = {"done", "infected_deleted", "error"}

    def __init__(self) -> None:
        self.settings = get_settings()
        self.qbt = QbtService()
        self.scanner = ScannerService()
        self.telegram = TelegramService()
        self.logger = logging.getLogger(__name__)

    def submit_job(self, db: Session, *, magnet_uri: str, final_parent: str, final_category: str | None,
                   staging_preference: str) -> Job:
        existing_torrent = self.qbt.find_existing_from_magnet(magnet_uri)
        if existing_torrent is not None:
            raise ValueError(
                self._duplicate_torrent_message(
                    db,
                    torrent_hash=getattr(existing_torrent, "hash", None),
                    torrent_name=getattr(existing_torrent, "name", None),
                    exclude_job_id=None,
                )
            )

        job_id = str(uuid4())
        unique_tag = f"ti_job_{uuid4().hex[:12]}"
        staging_root = self._root_for_preference(staging_preference)

        job = Job(
            id=job_id,
            magnet_uri=magnet_uri,
            final_parent=final_parent,
            final_category=final_category,
            staging_preference=staging_preference,
            staging_actual=staging_preference,
            staging_root_initial=staging_root,
            staging_root_actual=staging_root,
            managed_tag=self.settings.managed_tag,
            unique_tag=unique_tag,
            state="adding_to_qbt",
            updated_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        try:
            self.qbt.add_torrent(
                magnet_uri=magnet_uri,
                save_path=staging_root,
                tags=[self.settings.managed_tag, unique_tag],
                category=self.settings.intake_category,
            )
            self._resolve_hash_for_job(db, job)
        except TorrentAlreadyExistsError as exc:
            error_text = self._duplicate_torrent_message(
                db,
                torrent_hash=exc.torrent_hash,
                torrent_name=exc.torrent_name,
                exclude_job_id=job.id,
            )
            self.logger.warning("Rejected duplicate intake submit for job %s: %s", job.id, error_text)
            db.delete(job)
            db.commit()
            raise ValueError(error_text) from exc
        except Exception as exc:
            error_text = str(exc).strip() or repr(exc)
            self.logger.exception("Failed to submit job %s to qBittorrent", job.id)
            self._mark(job, "error", error=f"qBittorrent submission failed: {error_text}")
            db.add(job)
            db.commit()
            raise RuntimeError(f"Failed to submit to qBittorrent: {error_text}") from exc
        return job

    def retry_job(self, db: Session, *, job_id: str) -> Job:
        job = db.get(Job, job_id)
        if not job:
            raise LookupError("Job not found")
        if job.state != "error":
            raise ValueError("Only jobs in error state can be retried")

        staging_root = job.staging_root_actual or job.staging_root_initial or self._root_for_preference(job.staging_preference)
        self._prepare_job_for_retry(job)

        try:
            torrent = self.qbt.get_torrent(job.qbt_hash) if job.qbt_hash else None
            if job.qbt_hash and torrent is None:
                job.qbt_hash = None

            if not job.qbt_hash:
                self.qbt.add_torrent(
                    magnet_uri=job.magnet_uri,
                    save_path=staging_root,
                    tags=[self.settings.managed_tag, job.unique_tag],
                    category=self.settings.intake_category,
                )
                self._resolve_hash_for_job(db, job)
                return job

            self._sync_job_from_torrent(job, torrent)
            job.state = self._state_for_retry(job, torrent)
            job.is_terminal = False
            db.add(job)
            db.commit()
            db.refresh(job)
            return job
        except TorrentAlreadyExistsError as exc:
            message = self._duplicate_torrent_message(
                db,
                torrent_hash=exc.torrent_hash,
                torrent_name=exc.torrent_name,
                exclude_job_id=job.id,
            )
            existing_torrent = self.qbt.get_torrent(exc.torrent_hash) if exc.torrent_hash else None
            tracked_job = self._find_job_by_hash(db, torrent_hash=exc.torrent_hash, exclude_job_id=job.id)

            if existing_torrent is not None and tracked_job is None:
                job.qbt_hash = getattr(existing_torrent, "hash", None) or exc.torrent_hash
                self._sync_job_from_torrent(job, existing_torrent)
                job.state = self._state_for_retry(job, existing_torrent)
                job.is_terminal = False
                db.add(job)
                db.commit()
                db.refresh(job)
                self.logger.info(
                    "Attached retry job %s to existing qBittorrent torrent %s",
                    job.id,
                    job.qbt_hash,
                )
                return job

            self.logger.warning("Retry rejected for stale duplicate job %s: %s", job.id, message)
            self._mark(job, "error", error=message)
            db.add(job)
            db.commit()
            db.refresh(job)
            raise ValueError(message) from exc
        except Exception as exc:
            error_text = str(exc).strip() or repr(exc)
            self.logger.exception("Failed to retry job %s", job.id)
            self._mark(job, "error", error=f"retry failed: {error_text}")
            db.add(job)
            db.commit()
            raise RuntimeError(f"Failed to retry job: {error_text}") from exc

    def delete_job(self, db: Session, *, job_id: str) -> None:
        job = db.get(Job, job_id)
        if not job:
            raise LookupError("Job not found")
        if not job.is_terminal:
            raise ValueError("Only terminal jobs can be deleted")
        db.delete(job)
        db.commit()

    def retry_jobs(self, db: Session, *, job_ids: list[str]) -> dict[str, object]:
        return self._bulk_apply(job_ids, lambda selected_id: self.retry_job(db, job_id=selected_id))

    def delete_jobs(self, db: Session, *, job_ids: list[str]) -> dict[str, object]:
        return self._bulk_apply(job_ids, lambda selected_id: self.delete_job(db, job_id=selected_id))

    def delete_jobs_by_states(self, db: Session, *, states: set[str]) -> dict[str, object]:
        if not states:
            return self._empty_bulk_result()
        jobs = list(db.scalars(select(Job).where(Job.state.in_(tuple(states))).order_by(Job.created_at.desc())))
        return self.delete_jobs(db, job_ids=[job.id for job in jobs])

    def suggest_final_paths(self, prefix: str | None) -> list[str]:
        roots = [Path(path).resolve() for path in self.settings.allowed_final_parent_prefixes]
        default_root = Path(self.settings.final_parent_prefix.rstrip("/")).resolve()
        raw_prefix = (prefix or "").strip()
        normalized_prefix = raw_prefix or f"{default_root}/"

        suggestions: set[str] = set()
        suggestions.update(str(root) for root in roots)

        matched_root = self._matching_final_root(normalized_prefix, roots)
        if matched_root is None:
            filtered_roots = {
                suggestion for suggestion in suggestions
                if not raw_prefix or suggestion.lower().startswith(raw_prefix.lower())
            }
            return sorted(filtered_roots or suggestions)[:50]

        browse_dir, partial = self._path_lookup_context(normalized_prefix, matched_root)
        suggestions.update(self._list_child_directories(browse_dir, partial))

        exact_dir = Path(normalized_prefix.rstrip("/"))
        if normalized_prefix and exact_dir.exists() and exact_dir.is_dir() and self._is_within_root(str(exact_dir), matched_root):
            suggestions.update(self._list_child_directories(exact_dir, ""))

        filtered_suggestions = {
            suggestion for suggestion in suggestions
            if not raw_prefix or suggestion.lower().startswith(raw_prefix.lower())
        }
        return sorted(filtered_suggestions or suggestions)[:50]

    def _root_for_preference(self, preference: str) -> str:
        return self.settings.local_staging_root if preference == "local" else self.settings.nas_staging_root

    def _find_job_by_hash(self, db: Session, *, torrent_hash: str | None, exclude_job_id: str | None) -> Job | None:
        if not torrent_hash:
            return None
        jobs = list(db.scalars(select(Job).where(Job.qbt_hash == torrent_hash).order_by(Job.created_at.desc())))
        filtered = [job for job in jobs if job.id != exclude_job_id]
        if not filtered:
            return None
        for job in filtered:
            if not job.is_terminal:
                return job
        return filtered[0]

    def _duplicate_torrent_message(
        self,
        db: Session,
        *,
        torrent_hash: str | None,
        torrent_name: str | None,
        exclude_job_id: str | None,
    ) -> str:
        name_part = f"'{torrent_name}'" if torrent_name else "this torrent"
        hash_part = f" ({torrent_hash})" if torrent_hash else ""
        tracked_job = self._find_job_by_hash(db, torrent_hash=torrent_hash, exclude_job_id=exclude_job_id)
        if tracked_job is not None:
            return (
                f"{name_part}{hash_part} is already present in qBittorrent and is already tracked by intake job "
                f"{tracked_job.id}. Delete the stale intake row instead of retrying or re-adding it."
            )
        return (
            f"{name_part}{hash_part} is already present in qBittorrent. "
            "qBittorrent rejected the add because that torrent hash already exists."
        )

    def _empty_bulk_result(self) -> dict[str, object]:
        return {
            "requested": 0,
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "processed_ids": [],
            "skipped_ids": [],
            "failed_ids": [],
            "errors": {},
        }

    def _bulk_apply(self, job_ids: list[str], operation) -> dict[str, object]:
        unique_ids = list(dict.fromkeys(job_ids))
        result = self._empty_bulk_result()
        result["requested"] = len(unique_ids)

        for job_id in unique_ids:
            try:
                operation(job_id)
                result["processed_ids"].append(job_id)
            except (LookupError, ValueError) as exc:
                result["skipped_ids"].append(job_id)
                result["errors"][job_id] = str(exc)
            except RuntimeError as exc:
                result["failed_ids"].append(job_id)
                result["errors"][job_id] = str(exc)

        result["processed"] = len(result["processed_ids"])
        result["skipped"] = len(result["skipped_ids"])
        result["failed"] = len(result["failed_ids"])
        return result

    def _mark(self, job: Job, state: str, *, error: str | None = None) -> None:
        job.state = state
        job.updated_at = datetime.utcnow()
        job.last_error = error
        job.is_terminal = state in self.TERMINAL_STATES

    def _prepare_job_for_retry(self, job: Job) -> None:
        job.is_terminal = False
        job.last_error = None
        job.updated_at = datetime.utcnow()
        job.state = "retrying"

        # If the job failed before a successful scan/promotion, clear stale progress markers
        # so the worker doesn't jump back into a later phase with old timestamps/content paths.
        if not job.scan_completed_at:
            job.download_complete_at = None
            job.completion_event_received_at = None
            job.content_path = None
        if not job.promoted_at:
            job.scan_completed_at = None
        if not job.deleted_at:
            job.deleted_at = None

    def _sync_job_from_torrent(self, job: Job, torrent) -> None:
        if torrent is None:
            return
        job.torrent_name = getattr(torrent, "name", None) or job.torrent_name
        job.last_seen_qbt_state = getattr(torrent, "state", None)
        current_path = getattr(torrent, "content_path", None)
        if current_path:
            job.content_path = current_path
        size = getattr(torrent, "size", None) or getattr(torrent, "total_size", None)
        if isinstance(size, int):
            job.size_bytes = size

    def _state_for_retry(self, job: Job, torrent) -> str:
        if job.deleted_at:
            return "infected_deleted"
        if job.promoted_at:
            return "done"
        if job.scan_completed_at:
            return "promoting"
        if torrent is not None and self._is_torrent_complete(torrent):
            if not job.download_complete_at:
                job.download_complete_at = datetime.utcnow()
            return "download_complete"
        return "downloading"

    def _resolve_hash_for_job(self, db: Session, job: Job) -> None:
        torrent = self.qbt.find_by_unique_tag(job.unique_tag)
        if torrent is None:
            self._mark(job, "waiting_for_qbt_hash")
        else:
            job.qbt_hash = getattr(torrent, "hash", None)
            job.torrent_name = getattr(torrent, "name", None)
            job.last_seen_qbt_state = getattr(torrent, "state", None)
            size = getattr(torrent, "size", None) or getattr(torrent, "total_size", None)
            if isinstance(size, int):
                job.size_bytes = size
            self._mark(job, "downloading")
        db.add(job)
        db.commit()
        db.refresh(job)

    def ingest_completion_event(self, db: Session, *, qbt_hash: str | None, qbt_hash_v2: str | None,
                                unique_tag: str | None, tags: str | None, torrent_name: str | None,
                                content_path: str | None, root_path: str | None,
                                save_path: str | None, size_bytes: int | None) -> Job | None:
        unique_tag = unique_tag or self._extract_unique_tag(tags)
        stmt = None
        if qbt_hash:
            stmt = select(Job).where(Job.qbt_hash == qbt_hash)
        elif unique_tag:
            stmt = select(Job).where(Job.unique_tag == unique_tag)
        else:
            self.logger.warning(
                "Completion event ignored: no qbt_hash/unique_tag (qbt_hash_v2=%s, tags=%s, torrent_name=%s)",
                qbt_hash_v2,
                tags,
                torrent_name,
            )
            return None

        job = db.scalar(stmt)
        if not job:
            return None

        # Backdate by grace seconds so an event-triggered processing pass can act immediately.
        job.completion_event_received_at = datetime.utcnow() - timedelta(seconds=self.settings.completion_grace_seconds)
        if torrent_name:
            job.torrent_name = torrent_name
        event_path = content_path or root_path or save_path
        if event_path:
            job.content_path = event_path
        if isinstance(size_bytes, int) and size_bytes > 0:
            job.size_bytes = size_bytes
        self._mark(job, "completion_event_received")
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def process_job_immediately(self, db: Session, *, job_id: str, ignore_event_grace: bool = False) -> Job:
        job = db.get(Job, job_id)
        if not job:
            raise LookupError("Job not found")
        if job.is_terminal:
            return job
        try:
            self._process_one(db, job, ignore_event_grace=ignore_event_grace)
            db.refresh(job)
            return job
        except Exception as exc:
            self.logger.exception("Immediate processing failed for job %s", job.id)
            self._mark(job, "error", error=str(exc))
            db.add(job)
            db.commit()
            db.refresh(job)
            raise RuntimeError(str(exc)) from exc

    def process_nonterminal_jobs(self, db: Session) -> None:
        jobs = list(db.scalars(select(Job).where(Job.is_terminal == False)))
        for job in jobs:
            try:
                self._process_one(db, job, ignore_event_grace=False)
            except Exception as exc:
                self.logger.exception("Worker failed for job %s", job.id)
                self._mark(job, "error", error=str(exc))
                db.add(job)
                db.commit()

    def _process_one(self, db: Session, job: Job, *, ignore_event_grace: bool) -> None:
        if not job.qbt_hash:
            self._resolve_hash_for_job(db, job)
            return

        torrent = self.qbt.get_torrent(job.qbt_hash)
        if torrent is None:
            if job.state in {"infected_deleted", "done"}:
                job.is_terminal = True
                db.add(job)
                db.commit()
                return
            raise RuntimeError(f"Torrent {job.qbt_hash} not found in qBittorrent")

        qbt_state = getattr(torrent, "state", None)
        self._sync_job_from_torrent(job, torrent)

        self._maybe_override_to_nas(job)

        is_complete = self._is_torrent_complete(torrent)
        if is_complete and not job.download_complete_at:
            job.download_complete_at = datetime.utcnow()
            self._mark(job, "download_complete")

        event_ready = ignore_event_grace or (
            job.completion_event_received_at
            and datetime.utcnow() >= job.completion_event_received_at + timedelta(seconds=self.settings.completion_grace_seconds)
        )
        if (job.download_complete_at and job.state in {"download_complete", "completion_event_received", "downloading"}) and (event_ready or not job.completion_event_received_at):
            self._scan_and_finalize(job)

        db.add(job)
        db.commit()

    def _maybe_override_to_nas(self, job: Job) -> None:
        if job.staging_preference != "local":
            return
        if job.staging_actual != "local":
            return
        if job.size_bytes is None or job.size_bytes <= self.settings.local_max_bytes:
            return
        self.qbt.pause(job.qbt_hash)
        self.qbt.set_save_path(job.qbt_hash, self.settings.nas_staging_root)
        self.qbt.resume(job.qbt_hash)
        job.staging_actual = "nas"
        job.staging_root_actual = self.settings.nas_staging_root
        job.staging_overridden = True
        job.override_reason = "size_exceeds_threshold"
        self._mark(job, "downloading")

    def _scan_and_finalize(self, job: Job) -> None:
        if not job.content_path:
            torrent = self.qbt.get_torrent(job.qbt_hash)
            job.content_path = getattr(torrent, "content_path", None) or job.content_path
        if not job.content_path:
            raise RuntimeError("content_path is not available for completed torrent")

        self.qbt.pause(job.qbt_hash)
        self._mark(job, "scanning")
        self.logger.info("Scanning job %s path=%s", job.id, job.content_path)
        result = self.scanner.scan_path(job.content_path)
        job.scan_completed_at = datetime.utcnow()

        if result.infected:
            threat = result.threat_name or "unknown"
            self.qbt.delete_with_files(job.qbt_hash)
            job.threat_name = threat
            job.deleted_at = datetime.utcnow()
            self.telegram.send_infected_deleted(
                torrent_name=job.torrent_name,
                qbt_hash=job.qbt_hash,
                staging_path=job.content_path,
                final_parent=job.final_parent,
                threat_name=threat,
            )
            self._mark(job, "infected_deleted")
            self.logger.warning("Infected job deleted: job=%s threat=%s", job.id, threat)
            return

        self._mark(job, "promoting")
        self.logger.info("Promoting clean job %s to %s", job.id, job.final_parent)
        self.qbt.set_location(job.qbt_hash, job.final_parent)
        if job.final_category:
            resolved_category = self.qbt.resolve_or_create_category(
                job.final_category,
                create_if_missing=self.settings.auto_create_final_category,
            )
            if resolved_category != job.final_category:
                self.logger.info(
                    "Mapped final category for job %s from '%s' to existing '%s'",
                    job.id,
                    job.final_category,
                    resolved_category,
                )
            job.final_category = resolved_category
            self.qbt.set_category(job.qbt_hash, resolved_category)
        self.qbt.resume(job.qbt_hash)
        job.promoted_at = datetime.utcnow()
        self._mark(job, "done")
        self.logger.info("Job %s complete and resumed for seeding", job.id)

    def _is_torrent_complete(self, torrent) -> bool:
        progress = float(getattr(torrent, "progress", 0) or 0)
        amount_left = getattr(torrent, "amount_left", None)
        completion_on = getattr(torrent, "completion_on", 0) or 0
        qbt_state = str(getattr(torrent, "state", "") or "")
        state_enum = getattr(torrent, "state_enum", None)
        self.logger.info(
            "Completion check hash=%s state=%s progress=%.5f amount_left=%s completion_on=%s",
            getattr(torrent, "hash", "unknown"),
            qbt_state,
            progress,
            amount_left,
            completion_on,
        )

        if state_enum is not None:
            if getattr(state_enum, "is_downloading", False) or getattr(state_enum, "is_checking", False):
                return False
            if qbt_state in {"moving", "allocating", "missingFiles", "error", "unknown"}:
                return False

        if isinstance(amount_left, int) and amount_left > 0:
            return False

        if progress < 1.0:
            return False

        not_ready_states = {
            "downloading",
            "stalledDL",
            "forcedDL",
            "metaDL",
            "forcedMetaDL",
            "checkingDL",
            "checkingResumeData",
        }
        if qbt_state in not_ready_states:
            return False

        return bool(completion_on or progress >= 1.0)

    def _extract_unique_tag(self, tags: str | None) -> str | None:
        if not tags:
            return None
        for raw_tag in tags.split(","):
            tag = raw_tag.strip()
            if tag.startswith("ti_job_"):
                return tag
        return None

    def _path_lookup_context(self, typed_path: str, root: Path) -> tuple[Path, str]:
        if typed_path.endswith("/"):
            candidate = Path(typed_path.rstrip("/"))
            partial = ""
        else:
            candidate = Path(typed_path).parent
            partial = Path(typed_path).name

        browse_dir = candidate
        while browse_dir != root and (not browse_dir.exists() or not browse_dir.is_dir()):
            browse_dir = browse_dir.parent

        if not browse_dir.exists() or not browse_dir.is_dir():
            browse_dir = root

        return browse_dir, partial

    def _matching_final_root(self, typed_path: str, roots: list[Path]) -> Path | None:
        typed = typed_path.rstrip("/")
        matches = [
            root for root in roots
            if typed == str(root) or typed.startswith(f"{root}/")
        ]
        if not matches:
            return None
        return max(matches, key=lambda root: len(str(root)))

    def _list_child_directories(self, directory: Path, partial: str) -> set[str]:
        matches: set[str] = set()
        if not directory.exists() or not directory.is_dir():
            return matches

        lowered_partial = partial.lower()
        try:
            for entry in directory.iterdir():
                if not entry.is_dir():
                    continue
                if lowered_partial and not entry.name.lower().startswith(lowered_partial):
                    continue
                matches.add(str(entry))
        except OSError as exc:
            self.logger.warning("Failed to list suggestion directory %s: %s", directory, exc)
        return matches

    def _is_within_root(self, candidate: str, root: Path) -> bool:
        try:
            resolved = Path(candidate.rstrip("/")).resolve()
            return resolved == root or root in resolved.parents
        except OSError:
            return False
