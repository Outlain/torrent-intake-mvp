from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import get_settings
from .db import Base, engine, get_db
from .models import Job
from .schemas import CompletionEventIn, JobCreate, JobOut
from .service import JobService
from .worker import worker_loop

logging.basicConfig(
    level=logging.DEBUG if get_settings().debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

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


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui", status_code=307)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/jobs", response_model=list[JobOut])
def list_jobs(db: Session = Depends(get_db)):
    jobs = list(db.scalars(select(Job).order_by(Job.created_at.desc())))
    return jobs


@app.post("/jobs", response_model=JobOut)
def create_job(payload: JobCreate, db: Session = Depends(get_db)):
    try:
        job = service.submit_job(
            db,
            magnet_uri=payload.magnet_uri,
            final_parent=payload.final_parent,
            final_category=payload.final_category,
            staging_preference=payload.staging_preference,
        )
        return job
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: str, db: Session = Depends(get_db)):
    try:
        return service.retry_job(db, job_id=job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: str, db: Session = Depends(get_db)):
    try:
        service.delete_job(db, job_id=job_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
