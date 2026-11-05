"""任务集合选择：bench 配置读取与全量发现守卫，供 scripts 下的脚本共用。

座舱任务与 ESA 任务的环境服务不同，全量 glob 会把座舱任务扫进 ESA 环境服务并在
会话创建阶段报错。因此脚本统一用 ``--bench <slug>`` 按集合配置选任务；``--all``
保留全量发现，并加两道守卫：任务必须登记在某个集合里，且不得横跨多个集合。
"""

from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
BENCH_DIR = ROOT / "configs" / "bench"


def bench_slugs() -> list[str]:
    return sorted(path.stem for path in BENCH_DIR.glob("*.yaml") if not path.stem.startswith("_"))


def bench_tasks(slug: str) -> list[pathlib.Path]:
    path = BENCH_DIR / (slug + ".yaml")
    if not path.is_file():
        raise ValueError("未知任务集合：" + slug + "（可用：" + ", ".join(bench_slugs()) + "）")
    data = yaml.safe_load(path.read_text("utf-8")) or {}
    tasks = data.get("tasks")
    if tasks is None:
        tasks = []
    if not isinstance(tasks, list):
        raise ValueError("任务集合 " + slug + " 的 tasks 必须是列表")
    if not tasks:
        return []
    paths: list[pathlib.Path] = []
    for item in tasks:
        if not isinstance(item, dict) or not item.get("path"):
            raise ValueError("任务集合 " + slug + " 的 tasks 需要 path 字段")
        task = (ROOT / str(item["path"])).resolve()
        if not (task / "solution" / "reference.py").is_file():
            raise ValueError("任务不存在或缺少参考程序：" + str(item["path"]))
        paths.append(task)
    return paths


def registered_tasks() -> dict[pathlib.Path, list[str]]:
    """发现的任务到登记它的集合名的映射。"""
    mapping: dict[pathlib.Path, list[str]] = {}
    for slug in bench_slugs():
        for task in bench_tasks(slug):
            mapping.setdefault(task, []).append(slug)
    return mapping


def discover_tasks() -> list[pathlib.Path]:
    return sorted(path.parents[1] for path in (ROOT / "tasks").glob("*/solution/reference.py"))


def uses_cockpit_recorder(paths: list[pathlib.Path]) -> bool:
    """任务是否跑在座舱记录代理上：compose 里有 cockpit-mcp 服务。

    座舱任务的环境服务是 recorder，ESA 环境服务不接待座舱会话，验证脚本必须
    对这类任务走 ``--mock-url``，否则会在会话创建阶段报出指向参考解的错。
    """
    for task in paths:
        compose = task / "environment" / "docker-compose.yaml"
        if compose.is_file() and "cockpit-mcp" in compose.read_text("utf-8"):
            return True
    return False


def select_tasks(bench: str | None = None, use_all: bool = False) -> list[pathlib.Path]:
    """按集合选任务，或全量发现并执行登记与单集合两道守卫。"""
    if bench:
        return bench_tasks(bench)
    if not use_all:
        raise ValueError("必须指定 --bench 或 --all")
    registered = registered_tasks()
    discovered = discover_tasks()
    unregistered = [task for task in discovered if task not in registered]
    if unregistered:
        raise ValueError("以下任务没有登记在任何 bench 配置里："
                         + ", ".join(str(task.relative_to(ROOT)) for task in unregistered))
    slugs = {slug for task in discovered for slug in registered[task]}
    if len(slugs) > 1:
        raise ValueError("发现的任务横跨多个集合（" + ", ".join(sorted(slugs))
                         + "），请改用 --bench 逐集合执行")
    return discovered
