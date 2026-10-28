"""Console web entry: pages + progress API."""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx
from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config
from .db import init_db
from .services import (
    analysis_service,
    dataset_service,
    experiment_service,
    loop_service,
    profiles,
    review_service,
    scorer_service,
)

app = FastAPI(title="Console")
config.load_env()
config.ensure_workspace()
init_db()

app.mount("/static", StaticFiles(directory=config.CONSOLE_ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=str(config.CONSOLE_ROOT / "app" / "templates"))
templates.env.filters["from_json"] = json.loads


def _beautify_json(value):
    """Recursively parse JSON-in-string values so nested payloads lose escapes."""
    if isinstance(value, dict):
        return {k: _beautify_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_beautify_json(v) for v in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "["):
            try:
                return _beautify_json(json.loads(stripped))
            except json.JSONDecodeError:
                return value
    return value


templates.env.filters["pretty_json"] = lambda v: json.dumps(
    _beautify_json(v), ensure_ascii=False, indent=2
)


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
async def create_dataset(
    name: str = Form(...),
    description: str = Form(""),
    env_template: str = Form("standard"),
    csv_file: UploadFile | None = File(None),
) -> RedirectResponse:
    try:
        dataset_id = dataset_service.create_dataset(name, description, env_template)
        if csv_file is not None and csv_file.filename:
            text = (await csv_file.read()).decode("utf-8-sig")
            dataset_service.add_version(dataset_id, text, "初始版本")
    except dataset_service.DatasetError as exc:
        return _redirect("/datasets", str(exc))
    return _redirect(f"/datasets/{dataset_id}")


@app.post("/datasets/{dataset_id}/versions/rows")
def add_rows_version(
    dataset_id: int,
    payload: dict = Body(...),
) -> dict:
    try:
        meta = payload.get("meta") or {}
        if meta.get("name"):
            dataset_service.update_dataset(
                dataset_id,
                str(meta.get("name") or ""),
                str(meta.get("description") or ""),
                str(meta.get("env_template") or "standard"),
            )
        version_id = dataset_service.add_rows_version(
            dataset_id, payload.get("rows") or [], str(payload.get("change_note") or "")
        )
    except dataset_service.DatasetError as exc:
        return {"ok": False, "errors": str(exc).splitlines()}
    return {"ok": True, "version_id": version_id}


@app.post("/datasets/{dataset_id}/save-as")
def save_as_dataset(
    dataset_id: int,
    payload: dict = Body(...),
) -> dict:
    try:
        new_id = dataset_service.save_as(
            dataset_id,
            str(payload.get("name") or ""),
            str(payload.get("description") or ""),
            str(payload.get("env_template") or "standard"),
            payload.get("rows") or [],
        )
    except dataset_service.DatasetError as exc:
        return {"ok": False, "errors": str(exc).splitlines()}
    return {"ok": True, "dataset_id": new_id}


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


@app.post("/datasets/{dataset_id}")
def update_dataset(
    dataset_id: int,
    name: str = Form(...),
    description: str = Form(""),
    env_template: str = Form("standard"),
) -> RedirectResponse:
    try:
        dataset_service.update_dataset(dataset_id, name, description, env_template)
    except dataset_service.DatasetError as exc:
        return _redirect(f"/datasets/{dataset_id}", str(exc))
    return _redirect(f"/datasets/{dataset_id}")


@app.post("/datasets/{dataset_id}/delete")
def delete_dataset(dataset_id: int) -> RedirectResponse:
    try:
        dataset_service.delete_dataset(dataset_id)
    except dataset_service.DatasetError as exc:
        return _redirect("/datasets", str(exc))
    return _redirect("/datasets")


@app.get("/datasets/{dataset_id}/versions/{version_id}/export")
def export_version(dataset_id: int, version_id: int) -> Response:
    content = dataset_service.export_version_csv(version_id)
    return Response(
        content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="dataset-v{version_id}.csv"'
        },
    )


@app.get("/eval-profiles", response_class=HTMLResponse)
def profiles_page(request: Request) -> HTMLResponse:
    defaults = profiles.harness_defaults()
    first_harness = profiles.available_harnesses()[0]
    return templates.TemplateResponse(
        request,
        "eval_profiles.html",
        {
            "profiles": profiles.list_profiles(),
            "harnesses": profiles.available_harnesses(),
            "models": profiles.available_models(),
            "harness_defaults_json": json.dumps(defaults, ensure_ascii=False),
            "default_params_json": json.dumps(
                defaults.get(first_harness, {}), ensure_ascii=False, indent=2
            ),
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


@app.post("/eval-profiles/{profile_id}/delete")
def delete_profile(profile_id: int) -> RedirectResponse:
    try:
        profiles.delete_profile(profile_id)
    except profiles.ProfileError as exc:
        return _redirect("/eval-profiles", str(exc))
    return _redirect("/eval-profiles")


# ── Scorers ────────────────────────────────────────────────────────────


def _scorer_config_from_form(
    scorer_type: str,
    model_id: str,
    prompt_template: str,
    script_source: str,
    timeout_sec: str,
) -> dict:
    if scorer_type == "llm":
        return {
            "type": "llm",
            "model_id": model_id,
            "prompt_template": prompt_template,
        }
    return {
        "type": "code",
        "script_source": script_source,
        "timeout_sec": float(timeout_sec or 60),
    }


@app.get("/scorers", response_class=HTMLResponse)
def scorers_page(request: Request, tryout: int = 0, scorer: int = 0) -> HTMLResponse:
    completed = [e for e in experiment_service.list_experiments() if e["status"] == "completed"]
    detail = None
    if tryout:
        detail = scorer_service.get_tryout_detail(tryout)
    return templates.TemplateResponse(
        request,
        "scorers.html",
        {
            "scorers": scorer_service.list_scorers(),
            "models": profiles.available_models(),
            "experiments": completed,
            "tryouts": scorer_service.list_tryouts(),
            "tryout_detail": detail,
            "scorer_detail": scorer_service.get_scorer_detail(scorer) if scorer else None,
        },
    )


@app.post("/scorers")
def scorer_submit(
    action: str = Form("tryout"),
    name: str = Form(""),
    description: str = Form(""),
    change_note: str = Form(""),
    scorer_type: str = Form("llm"),
    model_id: str = Form(""),
    prompt_template: str = Form(""),
    script_source: str = Form(""),
    timeout_sec: str = Form("60"),
    experiment_id: int = Form(0),
    row_id: str = Form(""),
) -> RedirectResponse:
    try:
        config_dict = _scorer_config_from_form(
            scorer_type, model_id, prompt_template, script_source, timeout_sec
        )
        if action == "save":
            scorer_service.save(name, scorer_type, config_dict, description, change_note)
            return _redirect("/scorers")
        outcome = scorer_service.tryout(config_dict, experiment_id, row_id)
        return _redirect(f"/scorers?tryout={outcome['tryout_id']}")
    except (scorer_service.ScorerError, ValueError) as exc:
        return _redirect("/scorers", str(exc))


@app.post("/scorers/{scorer_id}/delete")
def delete_scorer(scorer_id: int) -> RedirectResponse:
    try:
        scorer_service.delete_scorer(scorer_id)
    except scorer_service.ScorerError as exc:
        return _redirect("/scorers", str(exc))
    return _redirect("/scorers")


@app.post("/scorers/tryouts/{tryout_id}/delete")
def delete_tryout(tryout_id: int) -> RedirectResponse:
    scorer_service.delete_tryout(tryout_id)
    return _redirect("/scorers")


# ── Experiments ────────────────────────────────────────────────────────


@app.get("/experiments/new", response_class=HTMLResponse)
def new_experiment_page(request: Request) -> HTMLResponse:
    datasets = dataset_service.list_datasets()
    return templates.TemplateResponse(
        request,
        "new_experiment.html",
        {
            "datasets": datasets,
            "dataset_versions": {
                d["id"]: dataset_service.list_versions(d["id"]) for d in datasets
            },
            "profiles": profiles.list_profiles(),
            "scorer_versions": scorer_service.list_all_versions(),
        },
    )


@app.get("/experiments", response_class=HTMLResponse)
def experiments_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "experiments.html",
        {"experiments": experiment_service.list_experiments()},
    )


@app.post("/experiments")
def create_experiment(
    name: str = Form(...),
    dataset_version_id: int = Form(...),
    eval_profile_id: int = Form(...),
    scorer_version_id: list[int] = Form(default=[]),
) -> RedirectResponse:
    try:
        experiment_id = experiment_service.create_experiment(
            name, dataset_version_id, eval_profile_id, scorer_version_id
        )
    except experiment_service.ExperimentError as exc:
        return _redirect("/experiments", str(exc))
    return _redirect(f"/experiments/{experiment_id}")


@app.get("/experiments/{experiment_id}", response_class=HTMLResponse)
def experiment_detail(request: Request, experiment_id: int) -> HTMLResponse:
    experiment = experiment_service.get_experiment(experiment_id)
    datasets = dataset_service.list_datasets()
    return templates.TemplateResponse(
        request,
        "experiment_detail.html",
        {
            "experiment": experiment,
            "tasks": experiment_service.list_tasks(experiment_id),
            "snapshot": json.loads(experiment["snapshot"]) if experiment else {},
            "datasets": datasets,
            "dataset_versions": {
                d["id"]: dataset_service.list_versions(d["id"]) for d in datasets
            },
            "profiles": profiles.list_profiles(),
            "scorer_versions": scorer_service.list_all_versions(),
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


@app.get("/api/experiments/{experiment_id}/tasks")
def experiment_task_rows(experiment_id: int) -> list[str]:
    return [t["row_id"] for t in experiment_service.list_tasks(experiment_id)]


# ── Analysis pages (M3) ────────────────────────────────────────────────


@app.get("/experiments/{experiment_id}/logs", response_class=HTMLResponse)
def experiment_logs(request: Request, experiment_id: int) -> HTMLResponse:
    experiment = experiment_service.get_experiment(experiment_id)
    tasks = sorted(
        experiment_service.list_tasks(experiment_id),
        key=lambda t: (t["score"] is None, t["score"] if t["score"] is not None else 0),
    )
    return templates.TemplateResponse(
        request, "logs.html", {"experiment": experiment, "tasks": tasks}
    )


@app.get("/experiments/{experiment_id}/traces/{row_id}", response_class=HTMLResponse)
def trace_detail(request: Request, experiment_id: int, row_id: str) -> HTMLResponse:
    experiment = experiment_service.get_experiment(experiment_id)
    task = next(
        (t for t in experiment_service.list_tasks(experiment_id) if t["row_id"] == row_id), None
    )
    detail = scorer_service.get_trace_detail(experiment, task) if task else None
    return templates.TemplateResponse(
        request,
        "trace_detail.html",
        {
            "experiment": experiment,
            "task": task,
            "detail": detail,
            "dimensions": review_service.dimensions(),
            "verdicts": review_service.VERDICTS,
        },
    )


@app.get("/experiments/{experiment_id}/dashboard", response_class=HTMLResponse)
def experiment_dashboard(request: Request, experiment_id: int, group_by: str = "tags") -> HTMLResponse:
    experiment = experiment_service.get_experiment(experiment_id)
    baseline = analysis_service.get_baseline(experiment_id)
    regression_data = None
    if baseline:
        try:
            regression_data = analysis_service.regression(experiment_id)
        except analysis_service.AnalysisError:
            regression_data = None
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "experiment": experiment,
            "dashboard": analysis_service.dashboard(experiment_id, group_by),
            "group_keys": analysis_service.group_keys(experiment_id),
            "baseline": baseline,
            "regression": regression_data,
            "is_baseline": analysis_service.is_baseline(experiment_id),
        },
    )


@app.post("/experiments/{experiment_id}/baseline")
def set_baseline(experiment_id: int) -> RedirectResponse:
    try:
        analysis_service.set_baseline(experiment_id)
    except analysis_service.AnalysisError as exc:
        return _redirect(f"/experiments/{experiment_id}/dashboard", str(exc))
    return _redirect(f"/experiments/{experiment_id}/dashboard")


@app.post("/experiments/{experiment_id}/baseline/clear")
def clear_baseline(experiment_id: int) -> RedirectResponse:
    try:
        analysis_service.clear_baseline(experiment_id)
    except analysis_service.AnalysisError as exc:
        return _redirect(f"/experiments/{experiment_id}/dashboard", str(exc))
    return _redirect(f"/experiments/{experiment_id}/dashboard")


@app.post("/experiments/{experiment_id}/delete")
def delete_experiment(experiment_id: int) -> RedirectResponse:
    try:
        experiment_service.delete_experiment(experiment_id)
    except experiment_service.ExperimentError as exc:
        return _redirect("/experiments", str(exc))
    return _redirect("/experiments")


@app.post("/experiments/{experiment_id}/duplicate")
def duplicate_experiment(
    experiment_id: int,
    name: str = Form(...),
) -> RedirectResponse:
    try:
        new_id = experiment_service.duplicate_experiment(experiment_id, name)
    except experiment_service.ExperimentError as exc:
        return _redirect(f"/experiments/{experiment_id}", str(exc))
    return _redirect(f"/experiments/{new_id}")


@app.post("/experiments/{experiment_id}")
def update_experiment(
    experiment_id: int,
    dataset_version_id: int = Form(...),
    eval_profile_id: int = Form(...),
    scorer_version_id: list[int] = Form(default=[]),
) -> RedirectResponse:
    try:
        experiment_service.update_draft(
            experiment_id, dataset_version_id, eval_profile_id, scorer_version_id
        )
    except experiment_service.ExperimentError as exc:
        return _redirect(f"/experiments/{experiment_id}", str(exc))
    return _redirect(f"/experiments/{experiment_id}")


@app.get("/experiments/{experiment_id}/export")
def export_experiment(experiment_id: int) -> Response:
    content = analysis_service.export_csv(experiment_id)
    return Response(
        content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="exp-{experiment_id}.csv"'
        },
    )


# ── Review (FR-6) ───────────────────────────────────────────────────────


@app.get("/review", response_class=HTMLResponse)
def review_page(
    request: Request, experiment: int = 0, state: str = ""
) -> HTMLResponse:
    reviews = review_service.list_reviews(
        experiment_id=experiment or None, state=state
    )
    experiments = experiment_service.list_experiments()
    summary = review_service.experiment_summary(experiment) if experiment else None
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "reviews": reviews,
            "experiments": experiments,
            "current_experiment": experiment,
            "current_state": state,
            "summary": summary,
            "verdict_labels": dict(review_service.VERDICTS),
        },
    )


@app.post("/api/reviews/flag")
def flag_review(payload: dict = Body(...)) -> dict:
    try:
        review_id = review_service.flag_task(int(payload.get("experiment_task_id") or 0))
    except review_service.ReviewError as exc:
        return {"ok": False, "errors": str(exc)}
    return {"ok": True, "review_id": review_id}


@app.post("/api/reviews/submit")
def submit_review(payload: dict = Body(...)) -> dict:
    try:
        review_id = review_service.submit_review(
            int(payload.get("experiment_task_id") or 0),
            payload.get("dims") or {},
            str(payload.get("overall_verdict") or ""),
            str(payload.get("note") or ""),
        )
    except review_service.ReviewError as exc:
        return {"ok": False, "errors": str(exc)}
    return {"ok": True, "review_id": review_id}


# ── Settings ────────────────────────────────────────────────────────────


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "settings.html", {"settings": review_service.get_settings()}
    )


@app.post("/settings")
async def update_settings(request: Request) -> RedirectResponse:
    form = await request.form()
    dimensions = [
        str(form.get(f"dimension-{i}") or "").strip()
        for i in range(int(form.get("dimension-count") or 0))
    ]
    dimensions = [name for name in dimensions if name]
    if not dimensions:
        return _redirect("/settings", "至少保留一个复核维度")
    updates = {
        "review_dimensions": dimensions,
        "blind_review": False,
        "loop_agent": str(form.get("loop_agent") or "opencode"),
        "loop_model": str(form.get("loop_model") or "stepcode/gpt-5.6-sol").strip(),
    }
    review_service.set_settings(updates)
    return _redirect("/settings")


# ── Loop-Assistant (FR-7) ───────────────────────────────────────────────


@app.get("/experiments/{experiment_id}/loop", response_class=HTMLResponse)
def loop_page(request: Request, experiment_id: int) -> HTMLResponse:
    experiment = experiment_service.get_experiment(experiment_id)
    if experiment is None:
        return _redirect("/experiments", f"实验不存在：{experiment_id}")
    runs = loop_service.list_runs(experiment_id)
    drafts = loop_service.list_drafts(runs[0]["id"]) if runs else []
    datasets = dataset_service.list_datasets()
    return templates.TemplateResponse(
        request,
        "loop.html",
        {
            "experiment": experiment,
            "runs": runs,
            "latest": runs[0] if runs else None,
            "drafts": drafts,
            "datasets": datasets,
            "n_failures": len(loop_service.get_failures(experiment_id)),
            "settings": review_service.get_settings(),
        },
    )


@app.post("/experiments/{experiment_id}/loop")
def start_loop(experiment_id: int) -> RedirectResponse:
    try:
        loop_service.start_run(experiment_id)
    except loop_service.LoopError as exc:
        return _redirect(f"/experiments/{experiment_id}/loop", str(exc))
    return _redirect(f"/experiments/{experiment_id}/loop")


@app.get("/api/loop/runs/{run_id}")
def loop_run_status(run_id: int) -> dict:
    run = loop_service.get_run(run_id)
    if run is None:
        return {"ok": False, "errors": "运行不存在"}
    return {"ok": True, "status": run["status"], "error": run["error"]}


@app.post("/api/loop/drafts/{draft_id}/rows")
def update_draft_rows(draft_id: int, payload: dict = Body(...)) -> dict:
    try:
        loop_service.update_draft_rows(draft_id, payload.get("rows") or [])
    except loop_service.LoopError as exc:
        return {"ok": False, "errors": str(exc)}
    return {"ok": True}


@app.post("/api/loop/drafts/{draft_id}/merge")
def merge_draft(draft_id: int, payload: dict = Body(...)) -> dict:
    try:
        version_id = loop_service.confirm_draft(
            draft_id, int(payload.get("target_dataset_id") or 0)
        )
    except (loop_service.LoopError, dataset_service.DatasetError) as exc:
        return {"ok": False, "errors": str(exc)}
    return {"ok": True, "version_id": version_id}


# ── Harbor view reverse proxy (registered last: console routes win) ────


_HARBOUR_STRIP_HEADERS = {
    "content-encoding", "content-length", "transfer-encoding",
    "x-frame-options", "content-security-policy",
    "etag", "last-modified",
}


def _proxy_to_harbor_view(
    path: str, request: Request, native_path: str | None = None
) -> Response:
    url = f"{config.HARBOUR_VIEW_BASE}/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    try:
        upstream = httpx.request(request.method, url, timeout=30)
    except httpx.HTTPError as exc:
        return Response(
            f"Harbor view 未运行或不可达（{config.HARBOUR_VIEW_BASE}）：{exc}",
            status_code=502,
            media_type="text/plain",
        )
    headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HARBOUR_STRIP_HEADERS
    }
    headers["cache-control"] = "no-store"
    content = upstream.content
    content_type = upstream.headers.get("content-type", "")
    if native_path and "text/html" in content_type:
        # The SPA router matches Harbor view's native paths; rewrite the
        # browser-visible URL before the router boots.
        inject = (
            f'<script>history.replaceState(null,"",'
            f'{json.dumps(native_path)})</script>'
        )
        text = content.decode("utf-8", "replace")
        if "<head>" in text:
            text = text.replace("<head>", "<head>" + inject, 1)
        else:
            text = inject + text
        content = text.encode()
    return Response(
        content,
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.get("/harbor-view")
@app.get("/harbor-view/{path:path}")
def harbor_view(request: Request, path: str = "") -> Response:
    native_path = "/" + path if path else "/"
    if request.url.query:
        native_path = f"{native_path}?{request.url.query}"
    return _proxy_to_harbor_view(path, request, native_path=native_path)


@app.get("/jobs")
@app.get("/jobs/{path:path}")
def harbor_view_jobs(request: Request, path: str = "") -> Response:
    return _proxy_to_harbor_view(f"jobs/{path}" if path else "jobs", request)


@app.get("/favicon.ico")
@app.get("/favicon.svg")
def harbor_view_favicon(request: Request) -> Response:
    return _proxy_to_harbor_view(request.url.path.lstrip("/"), request)


@app.get("/assets/{path:path}")
def harbor_view_assets(request: Request, path: str) -> Response:
    return _proxy_to_harbor_view(f"assets/{path}", request)


@app.get("/api/{path:path}")
def harbor_view_api_fallback(request: Request, path: str) -> Response:
    return _proxy_to_harbor_view(f"api/{path}", request)
