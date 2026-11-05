#!/usr/bin/env bash
# ESA 本地排障：只读检查镜像、compose 服务、日志、端口与最近 job。
# 不启动、不停止、不删除任何容器或镜像。
#
# 用法：
#   bash scripts/diagnose.sh                 # 检查全部 compose 项目
#   bash scripts/diagnose.sh -p <project>    # 只看指定项目
#   bash scripts/diagnose.sh -n 80           # 日志尾部行数
#   bash scripts/diagnose.sh --no-logs       # 只列状态，不打印日志
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT=""
TAIL_LINES=30
SHOW_LOGS=1
EXPECTED_SERVICES="main agent-api mock-system redis"
EXPECTED_COCKPIT_SERVICES="main mock-system cockpit-agent cockpit-mcp redis"

while [ $# -gt 0 ]; do
  case "$1" in
    -p|--project) PROJECT="${2:-}"; shift 2 ;;
    -n|--tail) TAIL_LINES="${2:-30}"; shift 2 ;;
    --no-logs) SHOW_LOGS=0; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; RESET=$'\033[0m'
else BOLD=""; DIM=""; RED=""; RESET=""; fi

section() { printf '\n%s%s%s\n' "$BOLD" "$1" "$RESET"; }
note() { printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; }

section "1. Docker"
if ! docker info >/dev/null 2>&1; then
  printf '  %sDocker 不可用，请先启动 Docker Desktop%s\n' "$RED" "$RESET"
  exit 1
fi
echo "  $(docker version --format '{{.Server.Version}}' 2>/dev/null | sed 's/^/daemon /')"

section "2. 公共镜像"
for image in esa-react-runtime:1.0.0 esa-local-environment:2.0.0; do
  if detail=$(docker image inspect "$image" --format '{{.Created}}' 2>/dev/null) && [ -n "$detail" ]; then
    echo "  OK    $image  (created ${detail%%.*})"
  else
    printf '  %s缺失  %s —— 运行 bash scripts/build-esa-local.sh%s\n' "$RED" "$image" "$RESET"
  fi
done

section "3. Compose 服务"
projects=$(docker ps -a --filter "label=com.docker.compose.project" \
  --format '{{.Label "com.docker.compose.project"}}' | sort -u)
if [ -n "$PROJECT" ]; then
  projects=$(printf '%s\n' "$projects" | awk -v keep="$PROJECT" '$0 == keep')
fi
if [ -z "$projects" ]; then
  note "没有运行中的 compose 项目（trial 已结束，或还没启动）"
else
  for project in $projects; do
    printf '\n  project: %s\n' "$project"
    rows=$(docker ps -a --filter "label=com.docker.compose.project=$project" \
      --format '{{.Label "com.docker.compose.service"}}|{{.Status}}|{{.Names}}' | sort)
    present=$(printf '%s\n' "$rows" | cut -d'|' -f1)
    printf '%s\n' "$rows" | while IFS='|' read -r service status name; do
      [ -z "$service" ] && continue
      restarts=$(docker inspect --format '{{.RestartCount}}' "$name" 2>/dev/null || echo '?')
      printf '    %-14s %-34s restarts=%s\n' "$service" "$status" "$restarts"
    done
    if printf '%s\n' "$present" | grep -qx main; then
      if printf '%s\n' "$present" | grep -qx cockpit-agent; then
        expected="$EXPECTED_COCKPIT_SERVICES"
      else
        expected="$EXPECTED_SERVICES"
      fi
      for service in $expected; do
        printf '%s\n' "$present" | grep -qx "$service" || note "缺少服务 $service"
      done
    else
      note "没有 main 服务：这是开发服务组合（esa chat），不是 trial 环境"
    fi
    if [ "$SHOW_LOGS" -eq 1 ]; then
      printf '%s\n' "$rows" | while IFS='|' read -r service status name; do
        [ -z "$name" ] && continue
        printf '\n    --- %s (last %s lines) ---\n' "$service" "$TAIL_LINES"
        docker logs --tail "$TAIL_LINES" "$name" 2>&1 | sed 's/^/    /'
      done
    fi
  done
fi

section "4. 宿主端口"
for port in 8003 18080 8080 8899; do
  owner=""
  if command -v lsof >/dev/null 2>&1; then
    owner=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1 " pid=" $2}')
  elif command -v ss >/dev/null 2>&1; then
    owner=$(ss -lntp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $4}')
  fi
  if [ -n "$owner" ]; then printf '  :%-6s %s\n' "$port" "$owner"
  else printf '  :%-6s 空闲\n' "$port"; fi
done

section "5. 最近 job"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  note "找不到 python，跳过 job 摘要"
else
  "$PY" - "$ROOT" <<'PY'
import glob, json, os, sys
root = sys.argv[1]
paths = sorted(glob.glob(os.path.join(root, "jobs", "*", "result.json")),
               key=os.path.getmtime)[-3:]
if not paths:
    print("  没有 job 记录")
    raise SystemExit
for path in reversed(paths):
    try:
        data = json.load(open(path, encoding="utf8"))
    except (OSError, ValueError):
        print("  %s  结果文件不可读" % os.path.basename(os.path.dirname(path)))
        continue
    stats = data.get("stats") or {}
    trials = data.get("n_total_trials")
    done = stats.get("n_completed_trials")
    errored = data.get("n_errored_trials", stats.get("n_errored_trials"))
    finished = data.get("finished_at") or "未完成"
    print("  %-42s trials=%s done=%s errored=%s  %s"
          % (os.path.basename(os.path.dirname(path)), trials, done, errored, finished))
    rewards = []
    for trial_reward in glob.glob(os.path.join(os.path.dirname(path), "*", "verifier", "reward.json")):
        try:
            rewards.append(json.load(open(trial_reward, encoding="utf8")).get("reward"))
        except (OSError, ValueError):
            pass
    if rewards:
        values = ", ".join("%.3f" % v if isinstance(v, (int, float)) else "?" for v in rewards)
        print("    reward: %s" % values)
PY
fi

section "6. 下一步"
echo "  - 服务 unhealthy 或缺失：看第 3 节对应服务的日志"
echo "  - 镜像缺失：bash scripts/build-esa-local.sh"
echo "  - 容器状态正常但 trial 失败：看 jobs/<job>/<trial>/verifier/score-detail.json"
echo "  - 需要完整轨迹：harbor view \"$ROOT/jobs\""
