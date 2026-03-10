from __future__ import annotations
from datetime import datetime, timedelta
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import get_settings
from .models import Job
from .qbt import QbtService
from .scanner import ScannerService
from .telegram import TelegramService


class JobService:
    TERMINAL_STATES = {"done", "infected_deleted", "error"}

    def __init__(self) -> None:
        self.settings = get_settings()
        self.qbt = QbtService()
        self.scanner = ScannerService()
        self.telegram = TelegramService()

    def submit_job(self, db: Session, *, magnet_uri: str, final_parent: str, final_category: str | None,
                   staging_preference: str) -> Job:
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
        except Exception as exc:
            self._mark(job, "error", error=f"qBittorrent submission failed: {exc}")
            db.add(job)
            db.commit()
            raise RuntimeError(f"Failed to submit to qBittorrent: {exc}") from exc
        return job

    def _root_for_preference(self, preference: str) -> str:
        return self.settings.local_staging_root if preference == "local" else self.settings.nas_staging_root

    def _mark(self, job: Job, state: str, *, error: str | None = None) -> None:
        job.state = state
        job.updated_at = datetime.utcnow()
        job.last_error = error
        job.is_terminal = state in self.TERMINAL_STATES

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

    def ingest_completion_event(self, db: Session, *, qbt_hash: str | None, unique_tag: str | None,
                                torrent_name: str | None, content_path: str | None) -> Job | None:
        stmt = None
        if qbt_hash:
            stmt = select(Job).where(Job.qbt_hash == qbt_hash)
        elif unique_tag:
            stmt = select(Job).where(Job.unique_tag == unique_tag)
        else:
            return None

        job = db.scalar(stmt)
        if not job:
            return None

        job.completion_event_received_at = datetime.utcnow()
        if torrent_name:
            job.torrent_name = torrent_name
        if content_path:
            job.content_path = content_path
        self._mark(job, "completion_event_received")
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def process_nonterminal_jobs(self, db: Session) -> None:
        jobs = list(db.scalars(select(Job).where(Job.is_terminal == False)))
        for job in jobs:
            try:
                self._process_one(db, job)
            except Exception as exc:
                self._mark(job, "error", error=str(exc))
                db.add(job)
                db.commit()

    def _process_one(self, db: Session, job: Job) -> None:
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
        job.last_seen_qbt_state = qbt_state
        job.torrent_name = getattr(torrent, "name", None) or job.torrent_name
        job.content_path = getattr(torrent, "content_path", None) or job.content_path
        size = getattr(torrent, "size", None) or getattr(torrent, "total_size", None)
        if isinstance(size, int):
            job.size_bytes = size

        self._maybe_override_to_nas(job)

        is_complete = bool(getattr(torrent, "progress", 0) == 1 or getattr(torrent, "completion_on", 0))
        if is_complete and not job.download_complete_at:
            job.download_complete_at = datetime.utcnow()
            self._mark(job, "download_complete")

        event_ready = job.completion_event_received_at and datetime.utcnow() >= job.completion_event_received_at + timedelta(seconds=self.settings.completion_grace_seconds)
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
            return

        self._mark(job, "promoting")
        self.qbt.set_location(job.qbt_hash, job.final_parent)
        if job.final_category:
            self.qbt.set_category(job.qbt_hash, job.final_category)
        self.qbt.resume(job.qbt_hash)
        job.promoted_at = datetime.utcnow()
        self._mark(job, "done")
