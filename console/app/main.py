"""Console web entry: pages + progress API."""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config
from .db import init_db
from .services import dataset_service, experiment_service, profiles

app = FastAPI(title="Console")
config.load_env()
config.ensure_workspace()
init_db()

app.mount("/static", StaticFiles(directory=config.CONSOLE_ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=str(config.CONSOLE_ROOT / "app" / "templates"))


def _redirect(path: str, error: str = "") -> RedirectResponse:
    suffix = f"?error={quote(error)}" if error else ""
    return RedirectResponse(f"{path}{suffix}", status_code=303)


@app.get("/", response_class=RedirectResponse)
def index() -> RedirectResponse:
    return RedirectResponse("/experiments", status_code=303)


# ── Datasets ───────────────────────────────────────────────────────────


@app.get("/datasets", response_class=HTMLResponse)
def datasets_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "datasets.html", {"datasets": dataset_service.list_datasets()}
    )


@app.post("/datasets")
def create_dataset(
    name: str = Form(...), description: str = Form(""), env_template: str = Form("standard")
) -> RedirectResponse:
    try:
        dataset_id = dataset_service.create_dataset(name, description, env_template)
    except dataset_service.DatasetError as exc:
        return _redirect("/datasets", str(exc))
    return _redirect(f"/datasets/{dataset_id}")


@app.get("/datasets/{dataset_id}", response_class=HTMLResponse)
def dataset_detail(request: Request, dataset_id: int) -> HTMLResponse:
    dataset = dataset_service.get_dataset(dataset_id)
    versions = dataset_service.list_versions(dataset_id)
    rows = dataset_service.list_rows(versions[0]["id"]) if versions else []
    return templates.TemplateResponse(
        request,
        "dataset_detail.html",
        {"dataset": dataset, "versions": versions, "rows": rows},
    )


@app.post("/datasets/{dataset_id}/versions")
async def add_version(
    dataset_id: int,
    csv_file: UploadFile = File(...),
    change_note: str = Form(""),
) -> RedirectResponse:
    try:
        text = (await csv_file.read()).decode("utf-8-sig")
        dataset_service.add_version(dataset_id, text, change_note)
    except dataset_service.DatasetError as exc:
        return _redirect(f"/datasets/{dataset_id}", str(exc))
    return _redirect(f"/datasets/{dataset_id}")


# ── Evaluation profiles ────────────────────────────────────────────────


@app.get("/eval-profiles", response_class=HTMLResponse)
def profiles_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "eval_profiles.html",
        {
            "profiles": profiles.list_profiles(),
            "harnesses": profiles.available_harnesses(),
            "models": profiles.available_models(),
        },
    )


@app.post("/eval-profiles")
def create_profile(
    name: str = Form(...),
    harness: str = Form(...),
    model_id: str = Form(...),
    params: str = Form("{}"),
) -> RedirectResponse:
    try:
        parsed = json.loads(params or "{}")
        if not isinstance(parsed, dict):
            raise profiles.ProfileError("params 必须是 JSON 对象")
        profiles.create_profile(name, harness, model_id, parsed)
    except (profiles.ProfileError, json.JSONDecodeError) as exc:
        return _redirect("/eval-profiles", str(exc))
    return _redirect("/eval-profiles")


# ── Experiments ────────────────────────────────────────────────────────


@app.get("/experiments", response_class=HTMLResponse)
def experiments_page(request: Request) -> HTMLResponse:
    datasets = dataset_service.list_datasets()
    profile_list = profiles.list_profiles()
    return templates.TemplateResponse(
        request,
        "experiments.html",
        {
            "experiments": experiment_service.list_experiments(),
            "datasets": datasets,
            "dataset_versions": {
                d["id"]: dataset_service.list_versions(d["id"]) for d in datasets
            },
            "profiles": profile_list,
        },
    )


@app.post("/experiments")
def create_experiment(
    name: str = Form(...),
    dataset_version_id: int = Form(...),
    eval_profile_id: int = Form(...),
) -> RedirectResponse:
    try:
        experiment_id = experiment_service.create_experiment(
            name, dataset_version_id, eval_profile_id
        )
    except experiment_service.ExperimentError as exc:
        return _redirect("/experiments", str(exc))
    return _redirect(f"/experiments/{experiment_id}")


@app.get("/experiments/{experiment_id}", response_class=HTMLResponse)
def experiment_detail(request: Request, experiment_id: int) -> HTMLResponse:
    experiment = experiment_service.get_experiment(experiment_id)
    return templates.TemplateResponse(
        request,
        "experiment_detail.html",
        {
            "experiment": experiment,
            "tasks": experiment_service.list_tasks(experiment_id),
            "snapshot": json.loads(experiment["snapshot"]) if experiment else {},
        },
    )


@app.post("/experiments/{experiment_id}/run")
def run_experiment(experiment_id: int) -> RedirectResponse:
    try:
        experiment_service.start(experiment_id)
    except experiment_service.ExperimentError as exc:
        return _redirect(f"/experiments/{experiment_id}", str(exc))
    return _redirect(f"/experiments/{experiment_id}")


@app.post("/experiments/{experiment_id}/abort")
def abort_experiment(experiment_id: int) -> RedirectResponse:
    try:
        experiment_service.abort(experiment_id)
    except experiment_service.ExperimentError as exc:
        return _redirect(f"/experiments/{experiment_id}", str(exc))
    return _redirect(f"/experiments/{experiment_id}")


@app.get("/api/experiments/{experiment_id}/progress")
def experiment_progress(experiment_id: int) -> dict:
    return experiment_service.progress(experiment_id)
