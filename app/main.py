from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import get_settings
from .db import Base, engine, get_db
from .models import Job
from .schemas import CompletionEventIn, JobCreate, JobOut
from .service import JobService
from .worker import worker_loop

settings = get_settings()
service = JobService()
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
worker_stop_event: asyncio.Event | None = None
worker_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker_stop_event, worker_task
    Base.metadata.create_all(bind=engine)
    worker_stop_event = asyncio.Event()
    worker_task = asyncio.create_task(worker_loop(worker_stop_event))
    yield
    if worker_stop_event:
        worker_stop_event.set()
    if worker_task:
        await worker_task


app = FastAPI(title=settings.ui_title, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/jobs", response_model=list[JobOut])
def list_jobs(db: Session = Depends(get_db)):
    jobs = list(db.scalars(select(Job).order_by(Job.created_at.desc())))
    return jobs


@app.post("/jobs", response_model=JobOut)
def create_job(payload: JobCreate, db: Session = Depends(get_db)):
    job = service.submit_job(
        db,
        magnet_uri=payload.magnet_uri,
        final_parent=payload.final_parent,
        final_category=payload.final_category,
        staging_preference=payload.staging_preference,
    )
    return job


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/events/qbt-complete")
def qbt_complete_event(payload: CompletionEventIn, db: Session = Depends(get_db)):
    job = service.ingest_completion_event(
        db,
        qbt_hash=payload.qbt_hash,
        unique_tag=payload.unique_tag,
        torrent_name=payload.torrent_name,
        content_path=payload.content_path,
    )
    if not job:
        raise HTTPException(status_code=404, detail="No matching job found")
    return {"status": "accepted", "job_id": job.id}


@app.post("/events/qbt-complete-form")
def qbt_complete_event_form(
    qbt_hash: str | None = Form(default=None),
    unique_tag: str | None = Form(default=None),
    torrent_name: str | None = Form(default=None),
    content_path: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    payload = CompletionEventIn(
        qbt_hash=qbt_hash,
        unique_tag=unique_tag,
        torrent_name=torrent_name,
        content_path=content_path,
    )
    return qbt_complete_event(payload, db)


@app.get("/ui", response_class=HTMLResponse)
def ui(request: Request, db: Session = Depends(get_db)):
    jobs = list(db.scalars(select(Job).order_by(Job.created_at.desc()).limit(50)))
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "title": settings.ui_title,
            "jobs": jobs,
            "settings": settings,
        },
    )
