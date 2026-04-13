#!/usr/bin/env python3
"""
Claude Code Token Dashboard

Parses Claude Code's local JSONL session transcripts and generates a
self-contained HTML dashboard showing token usage statistics.

Usage:
    python3 dashboard.py              # Generate static HTML file
    python3 dashboard.py --serve      # Start live dashboard server
    python3 dashboard.py --port 3000  # Custom port (default: 8080)
"""

import json
import os
import glob
import argparse
import subprocess
import webbrowser
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CONFIG_FILE  = os.path.expanduser("~/.claude-dashboard-config.json")

AGENT_PALETTE = ["#6c9bff", "#ff6cab", "#ffb86c", "#7ee8a0", "#c4a0ff", "#ff8a80", "#80d8ff", "#ffd54f"]
MODEL_PALETTE = {"opus": "#a855f7", "sonnet": "#6c9bff", "haiku": "#7ee8a0"}


# ---------------------------------------------------------------------------
# Data parsing
# ---------------------------------------------------------------------------

def detect_agent(filepath):
    """Detect agent name from the project directory path."""
    parts = Path(filepath).parts
    for part in parts:
        if part.startswith("-") and "-" in part[1:]:
            segments = part.strip("-").split("-")
            if segments:
                return segments[-1]
    return "default"


def get_session_id(filepath):
    """Extract the parent session UUID from a file path."""
    parts = Path(filepath).parts
    for i, part in enumerate(parts):
        if part == "subagents" and i > 0:
            return parts[i - 1]
    return Path(filepath).stem


def parse_jsonl_file(filepath):
    """Parse a single JSONL file and extract usage data."""
    entries = []
    first_timestamp = None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = data.get("timestamp")
                if ts and first_timestamp is None:
                    first_timestamp = ts

                msg = data.get("message")
                if not msg or not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not usage or not isinstance(usage, dict):
                    continue

                entries.append({
                    "input_tokens": usage.get("input_tokens", 0),
                    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "timestamp": data.get("timestamp", ts),
                    "model": msg.get("model", ""),
                })
    except (IOError, OSError):
        pass
    return entries, first_timestamp


def parse_timestamp(ts_str):
    """Parse an ISO timestamp string to a datetime object."""
    if not ts_str:
        return None
    try:
        ts_str = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def collect_all_data():
    """Scan all JSONL files and aggregate token data."""
    pattern = os.path.join(PROJECTS_DIR, "**", "*.jsonl")
    files = glob.glob(pattern, recursive=True)

    sessions = defaultdict(lambda: {
        "agent": "default",
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "entries": [],
        "models": defaultdict(int),
    })

    for filepath in files:
        agent = detect_agent(filepath)
        session_id = get_session_id(filepath)
        entries, file_timestamp = parse_jsonl_file(filepath)

        session = sessions[session_id]
        session["agent"] = agent

        ft = parse_timestamp(file_timestamp)
        if ft and (session["first_timestamp"] is None or ft < session["first_timestamp"]):
            session["first_timestamp"] = ft

        for entry in entries:
            session["input_tokens"] += entry["input_tokens"]
            session["cache_creation_input_tokens"] += entry["cache_creation_input_tokens"]
            session["cache_read_input_tokens"] += entry["cache_read_input_tokens"]
            session["output_tokens"] += entry["output_tokens"]
            if entry.get("model"):
                session["models"][entry["model"]] += 1

            et = parse_timestamp(entry.get("timestamp"))
            if et:
                if session["first_timestamp"] is None or et < session["first_timestamp"]:
                    session["first_timestamp"] = et
                if session["last_timestamp"] is None or et > session["last_timestamp"]:
                    session["last_timestamp"] = et

            session["entries"].append({
                **entry,
                "parsed_timestamp": et,
                "agent": agent,
            })

    return sessions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _model_color(name):
    for key, color in MODEL_PALETTE.items():
        if key in name:
            return color
    return "#8a8a9a"


def _model_short(name):
    if not name:
        return "\u2014"
    parts = name.replace("claude-", "").split("-")
    if len(parts) >= 3 and parts[1].isdigit():
        return f"{parts[0].title()} {parts[1]}.{parts[2]}"
    if len(parts) >= 2:
        return f"{parts[0].title()}"
    return name


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

# Context window sizes in tokens.
CONTEXT_WINDOWS = {
    "opus":   1_000_000,
    "sonnet":   200_000,
    "haiku":    200_000,
}
DEFAULT_CONTEXT_WINDOW = 200_000

# Per-million-token prices (USD). Cache read = 10% of input; cache write = 125% of input.
MODEL_PRICING = {
    "opus-4-6":   {"input": 15.00, "output": 75.00},
    "sonnet-4-6": {"input": 3.00,  "output": 15.00},
    "haiku-4-5":  {"input": 0.80,  "output": 4.00},
}


def _get_pricing(model):
    for key, pricing in MODEL_PRICING.items():
        if key in model:
            return pricing
    return None


def _entry_cost(model, input_t, output_t, cache_read_t, cache_write_t):
    """Estimate USD cost for a single API response."""
    p = _get_pricing(model)
    if not p:
        return 0.0
    return (
        (input_t      / 1_000_000) * p["input"] +
        (output_t     / 1_000_000) * p["output"] +
        (cache_read_t / 1_000_000) * p["input"] * 0.10 +
        (cache_write_t/ 1_000_000) * p["input"] * 1.25
    )


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_config():
    """Load dashboard config from ~/.claude-dashboard-config.json."""
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config):
    """Write config to disk."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def _agent_visible(name, config):
    return config.get("agents", {}).get(name, {}).get("visible", True)


def collect_agent_paths():
    """Return {agent_name: [decoded project-dir paths]} for every detected agent."""
    paths = defaultdict(set)
    pattern = os.path.join(PROJECTS_DIR, "**", "*.jsonl")
    for filepath in glob.glob(pattern, recursive=True):
        agent = detect_agent(filepath)
        for part in Path(filepath).parts:
            if part.startswith("-") and "-" in part[1:]:
                # Best-effort decode: strip leading dash, replace dashes → slashes
                decoded = "/" + part.lstrip("-").replace("-", "/")
                paths[agent].add(decoded)
                break
    return {k: sorted(v) for k, v in paths.items()}


def build_all_agents(config):
    """Build the complete agent list for the settings panel (unfiltered)."""
    agent_paths = collect_agent_paths()
    cfg_agents = config.get("agents", {})
    all_names = sorted(set(agent_paths) | set(cfg_agents))
    result = []
    for name in all_names:
        c = cfg_agents.get(name, {})
        result.append({
            "name": name,
            "display_name": c.get("display_name", ""),
            "visible": c.get("visible", True),
            "paths": agent_paths.get(name, []),
        })
    return result


# ---------------------------------------------------------------------------
# Agent health monitoring
# ---------------------------------------------------------------------------

IDLE_THRESHOLD_MINUTES = 10


def _fmt_uptime(seconds):
    """Format a duration in seconds to a human-readable uptime string."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h, m = divmod(seconds, 3600)
        return f"{h}h {m // 60}m"
    d, rem = divmod(seconds, 86400)
    return f"{d}d {rem // 3600}h"


def _fmt_ago(dt, now):
    """Format a datetime as 'Xs/Xm/Xh ago'."""
    secs = int((now - dt).total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _get_last_context_usage(filepath):
    """Read the tail of a JSONL file and return context window usage from the last message."""
    TAIL_BYTES = 32 * 1024
    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read().decode("utf-8", errors="replace")
        for line in reversed(chunk.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = data.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            model = msg.get("model", "")
            input_t = usage.get("input_tokens", 0) or 0
            cache_read_t = usage.get("cache_read_input_tokens", 0) or 0
            tokens_used = input_t + cache_read_t
            if tokens_used == 0:
                continue
            ctx_window = DEFAULT_CONTEXT_WINDOW
            for key, size in CONTEXT_WINDOWS.items():
                if key in model:
                    ctx_window = size
                    break
            pct = min(round((tokens_used / ctx_window) * 100, 1), 100.0)
            return {
                "tokens_used": tokens_used,
                "context_window": ctx_window,
                "model_short": _model_short(model),
                "pct": pct,
            }
    except (OSError, IOError):
        pass
    return None


def collect_agent_health():
    """Return a list of agent health records from tmux sessions and JSONL activity."""
    now = datetime.now().astimezone()

    # --- tmux sessions ---
    tmux_sessions = {}
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}:#{session_created}"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            if ":" in line:
                name, epoch_str = line.split(":", 1)
                try:
                    created = datetime.fromtimestamp(int(epoch_str)).astimezone()
                except (ValueError, OSError):
                    created = None
                tmux_sessions[name] = {"created": created}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # --- Last activity per agent from JSONL mtime; track the most-recent filepath too ---
    agent_last_activity = {}   # name -> (mtime datetime, filepath)
    pattern = os.path.join(PROJECTS_DIR, "**", "*.jsonl")
    for filepath in glob.glob(pattern, recursive=True):
        agent = detect_agent(filepath)
        if agent == "default":
            continue
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath)).astimezone()
            existing = agent_last_activity.get(agent)
            if existing is None or mtime > existing[0]:
                agent_last_activity[agent] = (mtime, filepath)
        except OSError:
            pass

    # --- Merge: all known agents from either source ---
    all_names = sorted(set(tmux_sessions) | set(agent_last_activity))

    records = []
    for name in all_names:
        entry = agent_last_activity.get(name)
        last_activity = entry[0] if entry else None
        latest_file   = entry[1] if entry else None
        tmux_info = tmux_sessions.get(name)

        if tmux_info is None:
            status = "offline"
        elif last_activity is None:
            status = "idle"
        else:
            minutes_since = (now - last_activity).total_seconds() / 60
            status = "active" if minutes_since <= IDLE_THRESHOLD_MINUTES else "idle"

        uptime = None
        session_started = None
        if tmux_info and tmux_info["created"]:
            uptime = _fmt_uptime(int((now - tmux_info["created"]).total_seconds()))
            session_started = tmux_info["created"].strftime("%H:%M")

        records.append({
            "name": name,
            "status": status,
            "last_activity": last_activity.strftime("%H:%M:%S") if last_activity else None,
            "last_activity_ts": last_activity.timestamp() if last_activity else None,
            "uptime": uptime,
            "session_started": session_started,
            "context": _get_last_context_usage(latest_file) if latest_file else None,
        })

    return records


# ---------------------------------------------------------------------------
# Line graph data (last N hours, 5-min buckets)
# ---------------------------------------------------------------------------

def compute_line_data(sessions, hours=5, bucket_minutes=5, config=None):
    """Return per-5-minute token usage for the last N hours, grouped by model and agent."""
    now = datetime.now().astimezone()
    cutoff = now - timedelta(hours=hours)
    n_buckets = (hours * 60) // bucket_minutes

    buckets_model = [defaultdict(int) for _ in range(n_buckets)]
    buckets_agent = [defaultdict(int) for _ in range(n_buckets)]
    buckets_model_cost = [defaultdict(float) for _ in range(n_buckets)]
    buckets_agent_cost = [defaultdict(float) for _ in range(n_buckets)]
    bucket_labels = []
    for i in range(n_buckets):
        t = cutoff + timedelta(minutes=i * bucket_minutes)
        bucket_labels.append(t.strftime("%H:%M"))

    all_models = set()
    all_agents = set()

    for sid, s in sessions.items():
        for entry in s.get("entries", []):
            et = entry.get("parsed_timestamp")
            if not et:
                continue
            et_local = et.astimezone()
            if et_local < cutoff or et_local > now:
                continue
            delta_min = (et_local - cutoff).total_seconds() / 60
            idx = int(delta_min // bucket_minutes)
            if not (0 <= idx < n_buckets):
                continue
            model = entry.get("model", "unknown")
            agent = entry.get("agent", s["agent"])
            if config and not _agent_visible(agent, config):
                continue
            total = entry["input_tokens"] + entry["output_tokens"] + entry["cache_creation_input_tokens"]
            ec = _entry_cost(model, entry["input_tokens"], entry["output_tokens"],
                             entry["cache_read_input_tokens"], entry["cache_creation_input_tokens"])
            buckets_model[idx][model] += total
            buckets_agent[idx][agent] += total
            buckets_model_cost[idx][model] += ec
            buckets_agent_cost[idx][agent] += ec
            all_models.add(model)
            all_agents.add(agent)

    serialized = []
    for i in range(n_buckets):
        serialized.append({
            "ts": bucket_labels[i],
            "by_model": dict(buckets_model[i]),
            "by_agent": dict(buckets_agent[i]),
            "cost_by_model": {k: round(v, 6) for k, v in buckets_model_cost[i].items()},
            "cost_by_agent": {k: round(v, 6) for k, v in buckets_agent_cost[i].items()},
        })

    return {
        "buckets": serialized,
        "models": sorted(all_models),
        "agents": sorted(all_agents),
    }


# ---------------------------------------------------------------------------
# Dashboard data computation
# ---------------------------------------------------------------------------

def compute_dashboard_data(sessions, config=None):
    """Compute all dashboard data and return as a JSON-serializable dict."""
    if config is None:
        config = {}
    now = datetime.now().astimezone()
    today = now.date()

    sorted_sessions_list = sorted(
        sessions.items(),
        key=lambda x: x[1]["first_timestamp"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    token_keys = ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"]
    daily = defaultdict(lambda: {**dict.fromkeys(token_keys, 0), "cost": 0.0})
    agent_today = defaultdict(lambda: {**dict.fromkeys(token_keys, 0), "cost": 0.0})
    session_costs = defaultdict(float)
    model_period_costs = defaultdict(lambda: {"today": 0.0, "week": 0.0, "all_time": 0.0})

    for sid, s in sorted_sessions_list:
        for entry in s.get("entries", []):
            # Entry-level agent filter: hidden agents contribute nothing to any metric
            entry_agent = entry.get("agent", s["agent"])
            if not _agent_visible(entry_agent, config):
                continue
            et = entry.get("parsed_timestamp") or s["first_timestamp"]
            if et is None:
                continue
            day = et.astimezone().date()
            model = entry.get("model", "")
            for k in token_keys:
                daily[day][k] += entry[k]
            ec = _entry_cost(
                model,
                entry["input_tokens"],
                entry["output_tokens"],
                entry["cache_read_input_tokens"],
                entry["cache_creation_input_tokens"],
            )
            daily[day]["cost"] += ec
            session_costs[sid] += ec
            if model:
                model_period_costs[model]["all_time"] += ec
                if (today - day).days < 7:
                    model_period_costs[model]["week"] += ec
                if day == today:
                    model_period_costs[model]["today"] += ec
            if day == today:
                for k in token_keys:
                    agent_today[entry_agent][k] += entry[k]
                agent_today[entry_agent]["cost"] += ec

    # Chart — last 7 days
    chart = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        t = daily.get(d, {**dict.fromkeys(token_keys, 0), "cost": 0.0})
        total = t["input_tokens"] + t["output_tokens"] + t["cache_creation_input_tokens"]
        chart.append({
            "label": d.strftime("%a"),
            "date_num": d.strftime("%d"),
            "total": total,
            "cost": round(t["cost"], 4),
            "is_today": d == today,
        })

    max_daily = max((d["total"] for d in chart), default=1) or 1
    for d in chart:
        d["bar_pct"] = max(round((d["total"] / max_daily) * 100, 1), 2)

    # Today stats
    td = daily.get(today, {**dict.fromkeys(token_keys, 0), "cost": 0.0})
    today_total = td["input_tokens"] + td["output_tokens"] + td["cache_creation_input_tokens"]
    today_cost = td["cost"]

    # All-time
    all_time = dict.fromkeys(token_keys, 0)
    all_time_cost = 0.0
    for dv in daily.values():
        for k in token_keys:
            all_time[k] += dv[k]
        all_time_cost += dv.get("cost", 0.0)
    all_time_total = all_time["input_tokens"] + all_time["output_tokens"] + all_time["cache_creation_input_tokens"]

    # Agent colors (assign palette index across ALL agents so colors stay stable when some are hidden)
    all_agents_unfiltered = sorted(set(s["agent"] for _, s in sorted_sessions_list))
    agent_colors = {agent: AGENT_PALETTE[i % len(AGENT_PALETTE)] for i, agent in enumerate(all_agents_unfiltered)}
    all_agents = [a for a in all_agents_unfiltered if _agent_visible(a, config)]

    # Agents today (visible only)
    agents_today_data = []
    for agent in all_agents:
        a = agent_today.get(agent, {**dict.fromkeys(token_keys, 0), "cost": 0.0})
        total = a["input_tokens"] + a["output_tokens"] + a["cache_creation_input_tokens"]
        agents_today_data.append({
            "name": agent,
            "total": total,
            "cost": round(a["cost"], 4),
            "bar_pct": round(min((total / max(today_total, 1)) * 100, 100), 1),
            "color": agent_colors.get(agent, "#8a8a9a"),
        })

    # Models (entry-level filter — same rule as stat aggregation)
    model_totals = defaultdict(lambda: dict.fromkeys(["input_tokens", "output_tokens", "cache_creation_input_tokens"], 0))
    for sid, s in sorted_sessions_list:
        for entry in s.get("entries", []):
            if not _agent_visible(entry.get("agent", s["agent"]), config):
                continue
            m = entry.get("model", "")
            if m:
                model_totals[m]["input_tokens"] += entry["input_tokens"]
                model_totals[m]["output_tokens"] += entry["output_tokens"]
                model_totals[m]["cache_creation_input_tokens"] += entry["cache_creation_input_tokens"]

    sorted_models = sorted(
        model_totals.items(),
        key=lambda x: x[1]["input_tokens"] + x[1]["output_tokens"] + x[1]["cache_creation_input_tokens"],
        reverse=True,
    )
    max_model = max(
        (m[1]["input_tokens"] + m[1]["output_tokens"] + m[1]["cache_creation_input_tokens"] for m in sorted_models),
        default=1,
    ) or 1

    models_data = []
    for model_name, m in sorted_models:
        total = m["input_tokens"] + m["output_tokens"] + m["cache_creation_input_tokens"]
        models_data.append({
            "name": model_name,
            "short": _model_short(model_name),
            "total": total,
            "cost": round(model_period_costs[model_name]["all_time"], 4),
            "bar_pct": round(min((total / max_model) * 100, 100), 1),
            "color": _model_color(model_name),
        })

    # Sessions (visible agents only)
    sessions_data = []
    for sid, s in sorted_sessions_list:
        if not _agent_visible(s["agent"], config):
            continue
        total = s["input_tokens"] + s["output_tokens"] + s["cache_creation_input_tokens"]
        ts = s["first_timestamp"]
        ts_local = ts.astimezone() if ts else None
        date_str = ts_local.strftime("%b %d, %H:%M") if ts_local else "\u2014"
        day = ts_local.date() if ts_local else None
        primary_model = max(s["models"], key=s["models"].get) if s["models"] else ""
        sessions_data.append({
            "id": sid[:12],
            "agent": s["agent"],
            "agent_color": agent_colors.get(s["agent"], "#8a8a9a"),
            "model_short": _model_short(primary_model),
            "model_color": _model_color(primary_model),
            "date": date_str,
            "input": s["input_tokens"],
            "output": s["output_tokens"],
            "cache_write": s["cache_creation_input_tokens"],
            "cache_read": s["cache_read_input_tokens"],
            "total": total,
            "cost": round(session_costs.get(sid, 0.0), 4),
            "is_today": day == today if day else False,
        })

    return {
        "stats": {
            "today_total": today_total,
            "today_input": td["input_tokens"],
            "today_output": td["output_tokens"],
            "today_cache": td["cache_creation_input_tokens"],
            "today_cost": round(today_cost, 4),
            "week_total": sum(d["total"] for d in chart),
            "week_cost": round(sum(d["cost"] for d in chart), 4),
            "all_time_total": all_time_total,
            "all_time_cost": round(all_time_cost, 4),
            "session_count": sum(1 for _, s in sorted_sessions_list if _agent_visible(s["agent"], config)),
            "model_costs_today": sorted(
                [{"name": m, "short": _model_short(m), "color": _model_color(m), "cost": round(v["today"], 4)}
                 for m, v in model_period_costs.items() if v["today"] > 0],
                key=lambda x: x["cost"], reverse=True),
            "model_costs_week": sorted(
                [{"name": m, "short": _model_short(m), "color": _model_color(m), "cost": round(v["week"], 4)}
                 for m, v in model_period_costs.items() if v["week"] > 0],
                key=lambda x: x["cost"], reverse=True),
            "model_costs_all_time": sorted(
                [{"name": m, "short": _model_short(m), "color": _model_color(m), "cost": round(v["all_time"], 4)}
                 for m, v in model_period_costs.items() if v["all_time"] > 0],
                key=lambda x: x["cost"], reverse=True),
        },
        "chart": chart,
        "agents_today": agents_today_data,
        "models": models_data,
        "sessions": sessions_data,
        "updated": now.strftime("%b %d, %Y at %H:%M"),
        "agent_colors": agent_colors,
        "line": compute_line_data(sessions, config=config),
        "agent_health": collect_agent_health(),
        "all_agents": build_all_agents(config),
        "config_file": CONFIG_FILE,
    }


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code \u2014 Token Dashboard</title>
<style>
    :root {
        --bg-primary: #0a0a0f;
        --bg-card: #12121a;
        --bg-card-hover: #16161f;
        --border: #1e1e2e;
        --border-subtle: #161622;
        --text-primary: #e8e8f0;
        --text-secondary: #6e6e82;
        --text-dim: #3a3a4a;
        --accent: #6c9bff;
        --accent-glow: rgba(108, 155, 255, 0.15);
        --gradient-start: #6c9bff;
        --gradient-end: #a855f7;
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', system-ui, sans-serif;
        background: var(--bg-primary);
        color: var(--text-primary);
        line-height: 1.5;
        -webkit-font-smoothing: antialiased;
    }
    .container { max-width: 1200px; margin: 0 auto; padding: 32px 24px; }

    /* Header */
    .header { margin-bottom: 32px; }
    .header h1 {
        font-size: 1.6rem;
        font-weight: 700;
        background: linear-gradient(135deg, var(--gradient-start), var(--gradient-end));
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        margin-bottom: 4px;
    }
    .header .meta {
        color: var(--text-secondary);
        font-size: 0.8rem;
        display: flex;
        align-items: center;
        gap: 6px;
    }
    .live-dot {
        display: inline-block;
        width: 6px;
        height: 6px;
        background: #7ee8a0;
        border-radius: 50%;
        flex-shrink: 0;
        animation: livepulse 2.4s ease-in-out infinite;
    }
    @keyframes livepulse {
        0%, 100% { opacity: 1; transform: scale(1); }
        50% { opacity: 0.35; transform: scale(0.8); }
    }
    .refresh-btn {
        background: none;
        border: 1px solid var(--border);
        color: var(--text-secondary);
        border-radius: 6px;
        padding: 2px 8px;
        font-size: 0.75rem;
        cursor: pointer;
        transition: border-color 0.15s, color 0.15s;
        line-height: 1.6;
    }
    .refresh-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
    .refresh-btn:disabled { opacity: 0.4; cursor: default; }
    .settings-btn {
        background: none;
        border: 1px solid var(--border);
        color: var(--text-secondary);
        border-radius: 6px;
        padding: 2px 7px;
        font-size: 0.82rem;
        cursor: pointer;
        transition: border-color 0.15s, color 0.15s;
        line-height: 1.6;
        margin-left: auto;
    }
    .settings-btn:hover { border-color: var(--accent); color: var(--accent); }

    /* Settings panel */
    .settings-overlay {
        position: fixed; inset: 0;
        background: rgba(0,0,0,0.55);
        z-index: 100;
        backdrop-filter: blur(2px);
    }
    .settings-panel {
        position: fixed;
        top: 0; right: 0; bottom: 0;
        width: 380px;
        background: #0e0e16;
        border-left: 1px solid var(--border);
        z-index: 101;
        display: flex;
        flex-direction: column;
        transform: translateX(100%);
        transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
        box-shadow: -16px 0 48px rgba(0,0,0,0.5);
    }
    .settings-panel.open { transform: translateX(0); }
    .settings-panel-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 20px 22px 16px;
        border-bottom: 1px solid var(--border);
        flex-shrink: 0;
    }
    .settings-panel-title {
        font-size: 0.9rem;
        font-weight: 700;
        color: var(--text-primary);
        letter-spacing: -0.01em;
    }
    .settings-close {
        background: none;
        border: 1px solid var(--border);
        color: var(--text-secondary);
        border-radius: 6px;
        width: 26px; height: 26px;
        display: flex; align-items: center; justify-content: center;
        font-size: 0.8rem;
        cursor: pointer;
        transition: border-color 0.15s, color 0.15s;
    }
    .settings-close:hover { border-color: var(--text-secondary); color: var(--text-primary); }
    .settings-body {
        flex: 1;
        overflow-y: auto;
        padding: 20px 22px;
        display: flex;
        flex-direction: column;
        gap: 16px;
    }
    .settings-body::-webkit-scrollbar { width: 4px; }
    .settings-body::-webkit-scrollbar-track { background: transparent; }
    .settings-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
    .settings-section-label {
        font-size: 0.65rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        color: var(--text-dim);
        margin-bottom: -8px;
    }
    .settings-hint {
        font-size: 0.72rem;
        color: var(--text-dim);
        line-height: 1.5;
        margin-bottom: -4px;
    }
    .settings-agent-list {
        display: flex;
        flex-direction: column;
        gap: 2px;
    }
    .settings-agent-row {
        display: flex;
        align-items: flex-start;
        gap: 12px;
        padding: 12px 14px;
        border-radius: 8px;
        border: 1px solid var(--border-subtle);
        background: var(--bg-primary);
        transition: border-color 0.15s;
    }
    .settings-agent-row:hover { border-color: var(--border); }
    .settings-agent-row.hidden-agent { opacity: 0.45; }
    /* Toggle switch */
    .toggle-wrap { flex-shrink: 0; padding-top: 2px; }
    .toggle-input { display: none; }
    .toggle-track {
        display: block;
        width: 34px; height: 19px;
        background: var(--border);
        border-radius: 10px;
        position: relative;
        cursor: pointer;
        transition: background 0.2s;
    }
    .toggle-input:checked + .toggle-track { background: var(--accent); }
    .toggle-thumb {
        position: absolute;
        top: 2px; left: 2px;
        width: 15px; height: 15px;
        background: #fff;
        border-radius: 50%;
        transition: transform 0.2s;
        box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    }
    .toggle-input:checked + .toggle-track .toggle-thumb { transform: translateX(15px); }
    /* Agent info */
    .settings-agent-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }
    .settings-agent-raw {
        font-size: 0.72rem;
        font-weight: 700;
        color: var(--text-primary);
        font-family: 'SF Mono', 'Fira Code', monospace;
    }
    .settings-name-input {
        background: var(--bg-card);
        border: 1px solid var(--border);
        border-radius: 6px;
        padding: 5px 9px;
        font-size: 0.72rem;
        color: var(--text-primary);
        width: 100%;
        transition: border-color 0.15s;
        font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', sans-serif;
    }
    .settings-name-input:focus { outline: none; border-color: var(--accent); }
    .settings-name-input:disabled { opacity: 0.4; cursor: not-allowed; }
    .settings-agent-paths {
        display: flex;
        flex-direction: column;
        gap: 2px;
    }
    .settings-agent-path {
        font-size: 0.6rem;
        color: var(--text-dim);
        font-family: 'SF Mono', 'Fira Code', monospace;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    /* Save footer */
    .settings-footer {
        border-top: 1px solid var(--border);
        padding: 16px 22px 20px;
        flex-shrink: 0;
        display: flex;
        flex-direction: column;
        gap: 8px;
    }
    .settings-autosave-hint {
        font-size: 0.68rem;
        color: var(--text-dim);
        text-align: center;
        letter-spacing: 0.01em;
    }
    .settings-config-path {
        font-size: 0.6rem;
        color: var(--text-dim);
        font-family: 'SF Mono', 'Fira Code', monospace;
        text-align: center;
    }

    /* Stats Grid */
    .stats-grid {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 16px;
        margin-bottom: 24px;
    }
    .stat-card {
        background: var(--bg-card);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 20px;
        transition: border-color 0.2s;
    }
    .stat-card:hover { border-color: var(--accent); }
    .stat-card-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        margin-bottom: 8px;
    }
    .stat-label {
        color: var(--text-secondary);
        font-size: 0.7rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
    }
    .stat-cost-badge {
        font-size: 0.7rem;
        font-weight: 700;
        color: #7ee8a0;
        background: rgba(126, 232, 160, 0.1);
        border: 1px solid rgba(126, 232, 160, 0.2);
        border-radius: 5px;
        padding: 2px 8px;
        letter-spacing: 0.01em;
        white-space: nowrap;
    }
    .stat-value {
        font-size: 2rem;
        font-weight: 800;
        color: var(--text-primary);
        letter-spacing: -0.02em;
    }
    .stat-detail { color: var(--text-secondary); font-size: 0.72rem; margin-top: 4px; }
    .stat-detail span { margin-right: 12px; }
    .stat-model-costs { margin-top: 12px; border-top: 1px solid var(--border-subtle); padding-top: 10px; display: flex; flex-direction: column; gap: 5px; }
    .model-cost-row { display: flex; align-items: center; gap: 6px; }
    .model-cost-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
    .model-cost-name { font-size: 0.68rem; color: var(--text-secondary); flex: 1; }
    .model-cost-val { font-size: 0.68rem; font-weight: 700; font-family: 'SF Mono', 'Fira Code', monospace; color: #7ee8a0; }

    .line-card-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 16px;
    }
    .tab-bar {
        display: flex;
        gap: 2px;
        background: var(--bg-primary);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 3px;
    }
    .tab-btn {
        background: none;
        border: none;
        color: var(--text-secondary);
        font-size: 0.7rem;
        font-weight: 600;
        padding: 4px 14px;
        border-radius: 6px;
        cursor: pointer;
        transition: background 0.15s, color 0.15s;
        letter-spacing: 0.04em;
    }
    .tab-btn.active { background: var(--bg-card-hover); color: var(--text-primary); }
    .tab-btn:hover:not(.active) { color: var(--text-primary); }
    .line-chart-wrap { position: relative; }
    .line-empty {
        text-align: center;
        color: var(--text-dim);
        padding: 40px 0;
        font-size: 0.82rem;
    }
    .line-legend {
        display: flex;
        flex-wrap: wrap;
        gap: 8px 20px;
        margin-top: 10px;
    }
    .legend-item { display: flex; align-items: center; gap: 6px; }
    .legend-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .legend-label { font-size: 0.7rem; color: var(--text-secondary); }

    /* Line tooltip */
    .line-tooltip {
        position: absolute;
        background: #16161f;
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 10px 13px;
        pointer-events: none;
        z-index: 20;
        min-width: 148px;
        box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    }
    .tt-time {
        font-size: 0.68rem;
        color: var(--text-secondary);
        font-family: 'SF Mono', 'Fira Code', monospace;
        font-weight: 600;
        margin-bottom: 7px;
        letter-spacing: 0.04em;
    }
    .tt-row { display: flex; align-items: center; gap: 7px; padding: 2px 0; }
    .tt-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
    .tt-label { font-size: 0.68rem; color: var(--text-secondary); flex: 1; }
    .tt-value {
        font-size: 0.72rem;
        font-weight: 700;
        font-family: 'SF Mono', 'Fira Code', monospace;
        color: var(--text-primary);
    }
    .tt-cost {
        font-size: 0.65rem;
        font-weight: 600;
        font-family: 'SF Mono', 'Fira Code', monospace;
        color: #7ee8a0;
        margin-left: 4px;
    }

    /* Main Grid */
    .main-grid {
        display: grid;
        grid-template-columns: 3fr 2fr;
        gap: 16px;
        margin-bottom: 24px;
    }
    .card {
        background: var(--bg-card);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 20px;
    }
    .card-title {
        color: var(--text-secondary);
        font-size: 0.7rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 16px;
    }

    /* 7-day Chart */
    .chart { display: flex; align-items: flex-end; gap: 6px; height: 200px; }
    .bar-group { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; }
    .bar-value {
        font-size: 0.6rem;
        color: var(--text-dim);
        margin-bottom: 6px;
        font-family: 'SF Mono', 'Fira Code', monospace;
        min-height: 14px;
    }
    .bar-track { flex: 1; width: 100%; display: flex; align-items: flex-end; }
    .bar {
        width: 100%;
        background: linear-gradient(180deg, var(--gradient-start), var(--gradient-end));
        border-radius: 4px 4px 1px 1px;
        min-height: 2px;
        opacity: 0.5;
        transition: height 0.4s ease, opacity 0.2s;
    }
    .bar-today { opacity: 1; box-shadow: 0 0 20px var(--accent-glow); }
    .bar-group:hover .bar { opacity: 0.8; }
    .bar-day { font-size: 0.7rem; color: var(--text-secondary); margin-top: 8px; font-weight: 500; }
    .bar-date { font-size: 0.65rem; color: var(--text-dim); }

    /* Agent/Model Breakdown */
    .agent-row {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 0;
        border-bottom: 1px solid var(--border-subtle);
    }
    .agent-row:last-child { border-bottom: none; }
    .agent-bar-wrap { flex: 1; height: 6px; background: var(--border-subtle); border-radius: 3px; overflow: hidden; }
    .agent-bar { height: 100%; border-radius: 3px; transition: width 0.4s ease; }
    .agent-total-wrap { display: flex; flex-direction: column; align-items: flex-end; min-width: 80px; }
    .agent-total {
        font-weight: 700;
        color: var(--text-primary);
        font-size: 0.85rem;
        text-align: right;
        font-family: 'SF Mono', 'Fira Code', monospace;
    }
    .agent-cost {
        font-size: 0.65rem;
        font-weight: 600;
        color: #7ee8a0;
        font-family: 'SF Mono', 'Fira Code', monospace;
        text-align: right;
    }
    .dim { color: var(--text-dim); font-weight: 400; }
    .badge {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 6px;
        font-size: 0.7rem;
        font-weight: 600;
        min-width: 64px;
        text-align: center;
        white-space: nowrap;
    }

    /* Sessions Table */
    .table-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
    .table-header { padding: 16px 20px 12px; }
    .table-scroll { max-height: 480px; overflow-y: auto; }
    .table-scroll::-webkit-scrollbar { width: 5px; }
    .table-scroll::-webkit-scrollbar-track { background: transparent; }
    .table-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
    thead th {
        text-align: left;
        padding: 10px 14px;
        color: var(--text-secondary);
        font-weight: 500;
        font-size: 0.68rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        border-bottom: 1px solid var(--border);
        background: var(--bg-primary);
        position: sticky;
        top: 0;
        z-index: 1;
    }
    tbody td { padding: 10px 14px; border-bottom: 1px solid var(--border-subtle); }
    tbody tr { transition: background 0.15s; }
    tbody tr:hover { background: var(--bg-card-hover); }
    tr.today { background: rgba(108, 155, 255, 0.04); }
    tr.today:hover { background: rgba(108, 155, 255, 0.08); }
    .mono { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.73rem; color: var(--text-secondary); }
    .num { text-align: right; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.73rem; }
    .bold { font-weight: 700; color: var(--text-primary); }
    .cost-cell { color: #7ee8a0; font-weight: 600; }

    /* Agent Health Monitor */
    .health-card {
        background: var(--bg-card);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 24px;
    }
    .health-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
        gap: 12px;
        margin-top: 4px;
    }
    .health-agent {
        background: var(--bg-primary);
        border: 1px solid var(--border-subtle);
        border-radius: 10px;
        padding: 14px 16px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        transition: border-color 0.2s;
    }
    .health-agent:hover { border-color: var(--border); }
    .health-agent-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
    }
    .health-agent-name {
        font-size: 0.82rem;
        font-weight: 700;
        color: var(--text-primary);
        text-transform: capitalize;
    }
    .health-status {
        display: flex;
        align-items: center;
        gap: 5px;
        padding: 2px 8px;
        border-radius: 5px;
        font-size: 0.62rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        white-space: nowrap;
    }
    .health-status-dot {
        width: 5px;
        height: 5px;
        border-radius: 50%;
        flex-shrink: 0;
    }
    .health-status.active {
        background: rgba(126, 232, 160, 0.1);
        color: #7ee8a0;
        border: 1px solid rgba(126, 232, 160, 0.2);
    }
    .health-status.active .health-status-dot { background: #7ee8a0; animation: livepulse 2.4s ease-in-out infinite; }
    .health-status.idle {
        background: rgba(255, 184, 108, 0.1);
        color: #ffb86c;
        border: 1px solid rgba(255, 184, 108, 0.2);
    }
    .health-status.idle .health-status-dot { background: #ffb86c; }
    .health-status.offline {
        background: rgba(58, 58, 74, 0.4);
        color: var(--text-dim);
        border: 1px solid var(--border-subtle);
    }
    .health-status.offline .health-status-dot { background: var(--text-dim); }
    .health-meta {
        display: flex;
        flex-direction: column;
        gap: 3px;
    }
    .health-meta-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 8px;
    }
    .health-meta-label {
        font-size: 0.62rem;
        color: var(--text-dim);
        text-transform: uppercase;
        letter-spacing: 0.06em;
    }
    .health-meta-val {
        font-size: 0.68rem;
        font-weight: 600;
        color: var(--text-secondary);
        font-family: 'SF Mono', 'Fira Code', monospace;
    }
    .health-ctx { margin-top: 10px; border-top: 1px solid var(--border-subtle); padding-top: 8px; }
    .health-ctx-bar-track {
        height: 4px;
        background: var(--border-subtle);
        border-radius: 2px;
        overflow: hidden;
        margin-bottom: 5px;
    }
    .health-ctx-bar {
        height: 100%;
        border-radius: 2px;
        transition: width 0.4s ease, background 0.3s ease;
    }
    .health-ctx-label {
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .health-ctx-tokens {
        font-size: 0.62rem;
        color: var(--text-dim);
        font-family: 'SF Mono', 'Fira Code', monospace;
    }
    .health-ctx-pct {
        font-size: 0.68rem;
        font-weight: 700;
        font-family: 'SF Mono', 'Fira Code', monospace;
    }

    @media (max-width: 768px) {
        .stats-grid { grid-template-columns: 1fr; }
        .main-grid { grid-template-columns: 1fr; }
        .stat-value { font-size: 1.5rem; }
        .health-grid { grid-template-columns: 1fr 1fr; }
    }
</style>
</head>
<body>
<div id="app" class="container">

    <div class="header">
        <h1>Claude Code Token Dashboard</h1>
        <div class="meta">
            <span v-if="serveMode" class="live-dot"></span>
            <span>Updated {{ updated }} &middot; {{ stats.session_count }} sessions</span>
            <button v-if="serveMode" @click="refresh" class="refresh-btn" :disabled="refreshing">
                {{ refreshing ? '\u2026' : '\u21bb' }}
            </button>
            <button @click="openSettings" class="settings-btn" title="Settings">\u2699</button>
        </div>
    </div>

    <!-- Settings overlay + panel -->
    <div class="settings-overlay" v-if="settingsOpen" @click="closeSettings"></div>
    <div class="settings-panel" :class="{ open: settingsOpen }">
        <div class="settings-panel-head">
            <span class="settings-panel-title">\u2699 Settings</span>
            <button class="settings-close" @click="closeSettings">\u2715</button>
        </div>
        <div class="settings-body">
            <div class="settings-section-label">Agents</div>
            <p class="settings-hint">Toggle visibility to exclude agents from all metrics. Set a display name to override the auto-detected label.</p>
            <div class="settings-agent-list">
                <div v-for="a in agentDraft" :key="a.name"
                     class="settings-agent-row" :class="{ 'hidden-agent': !a.visible }">
                    <label class="toggle-wrap">
                        <input class="toggle-input" type="checkbox" :checked="a.visible"
                               @change="a.visible = $event.target.checked; autoSave(0)">
                        <span class="toggle-track"><span class="toggle-thumb"></span></span>
                    </label>
                    <div class="settings-agent-info">
                        <span class="settings-agent-raw">{{ a.name }}</span>
                        <input class="settings-name-input"
                               :placeholder="'Display name (default: ' + a.name + ')'"
                               v-model="a.display_name"
                               @input="autoSave(800)"
                               :disabled="!a.visible">
                        <div class="settings-agent-paths" v-if="a.paths && a.paths.length">
                            <span v-for="p in a.paths" :key="p" class="settings-agent-path" :title="p">{{ p }}</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <div class="settings-footer">
            <div class="settings-autosave-hint">
                {{ serveMode ? 'Changes apply instantly' : 'Live updates require server mode' }}
            </div>
            <div class="settings-config-path">{{ configFile }}</div>
        </div>
    </div>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-card-head">
                <div class="stat-label">Today</div>
                <div class="stat-cost-badge">{{ fmtCost(stats.today_cost) }}</div>
            </div>
            <div class="stat-value">{{ fmt(stats.today_total) }}</div>
            <div class="stat-detail">
                <span>In: {{ fmt(stats.today_input) }}</span>
                <span>Out: {{ fmt(stats.today_output) }}</span>
                <span>Cache: {{ fmt(stats.today_cache) }}</span>
            </div>
            <div class="stat-model-costs" v-if="stats.model_costs_today && stats.model_costs_today.length">
                <div class="model-cost-row" v-for="mc in stats.model_costs_today" :key="mc.name">
                    <span class="model-cost-dot" :style="{ background: mc.color }"></span>
                    <span class="model-cost-name">{{ mc.short }}</span>
                    <span class="model-cost-val">{{ fmtCost(mc.cost) }}</span>
                </div>
            </div>
        </div>
        <div class="stat-card">
            <div class="stat-card-head">
                <div class="stat-label">This Week</div>
                <div class="stat-cost-badge">{{ fmtCost(stats.week_cost) }}</div>
            </div>
            <div class="stat-value">{{ fmt(stats.week_total) }}</div>
            <div class="stat-detail">Last 7 days</div>
            <div class="stat-model-costs" v-if="stats.model_costs_week && stats.model_costs_week.length">
                <div class="model-cost-row" v-for="mc in stats.model_costs_week" :key="mc.name">
                    <span class="model-cost-dot" :style="{ background: mc.color }"></span>
                    <span class="model-cost-name">{{ mc.short }}</span>
                    <span class="model-cost-val">{{ fmtCost(mc.cost) }}</span>
                </div>
            </div>
        </div>
        <div class="stat-card">
            <div class="stat-card-head">
                <div class="stat-label">All Time</div>
                <div class="stat-cost-badge">{{ fmtCost(stats.all_time_cost) }}</div>
            </div>
            <div class="stat-value">{{ fmt(stats.all_time_total) }}</div>
            <div class="stat-detail">{{ stats.session_count }} sessions</div>
            <div class="stat-model-costs" v-if="stats.model_costs_all_time && stats.model_costs_all_time.length">
                <div class="model-cost-row" v-for="mc in stats.model_costs_all_time" :key="mc.name">
                    <span class="model-cost-dot" :style="{ background: mc.color }"></span>
                    <span class="model-cost-name">{{ mc.short }}</span>
                    <span class="model-cost-val">{{ fmtCost(mc.cost) }}</span>
                </div>
            </div>
        </div>
    </div>

    <!-- Agent Health Monitor -->
    <div class="health-card" v-if="agent_health && agent_health.length">
        <div class="card-title">Agent Health</div>
        <div class="health-grid">
            <div v-for="agent in agent_health" :key="agent.name" class="health-agent">
                <div class="health-agent-head">
                    <span class="health-agent-name">{{ displayName(agent.name) }}</span>
                    <span class="health-status" :class="agent.status">
                        <span class="health-status-dot"></span>
                        {{ agent.status }}
                    </span>
                </div>
                <div class="health-meta">
                    <div class="health-meta-row">
                        <span class="health-meta-label">Last activity</span>
                        <span class="health-meta-val" :style="agent.last_activity_ts ? {} : { color: 'var(--text-dim)' }">
                            {{ fmtAgo(agent.last_activity_ts) }}
                        </span>
                    </div>
                    <div class="health-meta-row" v-if="agent.uptime">
                        <span class="health-meta-label">Uptime</span>
                        <span class="health-meta-val">{{ agent.uptime }}</span>
                    </div>
                    <div class="health-meta-row" v-if="agent.session_started">
                        <span class="health-meta-label">Started</span>
                        <span class="health-meta-val">{{ agent.session_started }}</span>
                    </div>
                </div>
                <div class="health-ctx" v-if="agent.context">
                    <div class="health-ctx-bar-track">
                        <div class="health-ctx-bar"
                             :style="{ width: agent.context.pct + '%', background: ctxBarColor(agent.context.pct) }">
                        </div>
                    </div>
                    <div class="health-ctx-label">
                        <span class="health-ctx-tokens">
                            {{ fmtCtx(agent.context.tokens_used) }} / {{ fmtCtx(agent.context.context_window) }}
                            <span style="color: var(--text-dim); font-weight:400"> ctx</span>
                        </span>
                        <span class="health-ctx-pct" :style="{ color: ctxBarColor(agent.context.pct) }">
                            {{ agent.context.pct }}%
                        </span>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Main Grid: 2 cols × 2 rows — line/agent top, bar/model bottom -->
    <div class="main-grid">

        <!-- Row 1, Col 1: Activity Line Graph -->
        <div class="card line-card">
            <div class="line-card-header">
                <div class="card-title" style="margin-bottom:0">Activity \u2014 Last 5 Hours</div>
                <div class="tab-bar">
                    <button class="tab-btn" :class="{ active: lineTab === 'model' }" @click="lineTab = 'model'">Model</button>
                    <button class="tab-btn" :class="{ active: lineTab === 'agent' }" @click="lineTab = 'agent'">Agent</button>
                </div>
            </div>
            <div v-if="lineMaxY === 0" class="line-empty">No activity in the last 5 hours</div>
            <div v-else class="line-chart-wrap">
                <svg viewBox="0 0 1000 168" width="100%" height="168"
                     style="display:block; overflow:visible; cursor:crosshair;"
                     @mousemove="onLineHover($event)"
                     @mouseleave="onLineLeave()">
                    <line v-for="n in 4" :key="n"
                          x1="0" :y1="10 + (n / 4) * 120" x2="1000" :y2="10 + (n / 4) * 120"
                          stroke="#1e1e2e" stroke-width="0.8" />
                    <polygon v-for="s in linePaths" :key="s.key + '_fill'"
                             :points="s.fillPoints" :fill="s.color + '14'" />
                    <polyline v-for="s in linePaths" :key="s.key"
                              :points="s.points" :stroke="s.color"
                              fill="none" stroke-width="1.8"
                              stroke-linejoin="round" stroke-linecap="round" />
                    <line v-if="hoverIdx !== null"
                          :x1="hoverVBX" y1="10" :x2="hoverVBX" y2="130"
                          stroke="rgba(255,255,255,0.12)" stroke-width="1" stroke-dasharray="3,4" />
                    <circle v-if="hoverIdx !== null"
                            v-for="s in linePaths" :key="s.key + '_dot'"
                            :cx="hoverVBX" :cy="getHoverY(s.key)"
                            r="5" :fill="s.color" stroke="#0a0a0f" stroke-width="2" />
                    <text v-for="lbl in lineXLabels" :key="lbl.x"
                          :x="lbl.x" y="164" fill="#3a3a4a" font-size="22"
                          text-anchor="middle" font-family="'SF Mono','Fira Code',monospace">{{ lbl.text }}</text>
                </svg>
                <div class="line-tooltip" :style="tooltipStyle" v-if="hoverIdx !== null">
                    <div class="tt-time">{{ hoverBucket && hoverBucket.ts }}</div>
                    <div v-for="v in hoverValues" :key="v.label" class="tt-row">
                        <span class="tt-dot" :style="{ background: v.color }"></span>
                        <span class="tt-label">{{ v.label }}</span>
                        <span class="tt-value">{{ fmt(v.value) }}</span>
                        <span class="tt-cost" v-if="v.cost > 0">{{ fmtCost(v.cost) }}</span>
                    </div>
                </div>
                <div class="line-legend">
                    <div v-for="s in linePaths" :key="s.key + '_leg'" class="legend-item">
                        <span class="legend-dot" :style="{ background: s.color }"></span>
                        <span class="legend-label">{{ s.label }}</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Row 1, Col 2: Today by Agent -->
        <div class="card">
            <div class="card-title">Today by Agent</div>
            <div v-if="agents_today.length === 0" class="dim" style="padding:20px 0;text-align:center">No usage today</div>
            <div v-for="agent in agents_today" :key="agent.name" class="agent-row">
                <span class="badge" :style="badgeStyle(agent.color)">{{ displayName(agent.name) }}</span>
                <template v-if="agent.total > 0">
                    <div class="agent-bar-wrap">
                        <div class="agent-bar" :style="{ width: agent.bar_pct + '%', background: agent.color + '40' }"></div>
                    </div>
                    <div class="agent-total-wrap">
                        <span class="agent-total">{{ fmt(agent.total) }}</span>
                        <span class="agent-cost" v-if="agent.cost > 0">{{ fmtCost(agent.cost) }}</span>
                    </div>
                </template>
                <template v-else>
                    <div class="agent-total-wrap"><span class="agent-total dim">\u2014</span></div>
                </template>
            </div>
        </div>

        <!-- Row 2, Col 1: Last 7 Days Bar Chart -->
        <div class="card">
            <div class="card-title">Last 7 Days</div>
            <div class="chart">
                <div class="bar-group" v-for="day in chart" :key="day.label">
                    <div class="bar-value">{{ fmt(day.total) }}</div>
                    <div class="bar-track">
                        <div class="bar" :class="{ 'bar-today': day.is_today }" :style="{ height: day.bar_pct + '%' }"></div>
                    </div>
                    <div class="bar-day">{{ day.label }}</div>
                    <div class="bar-date">{{ day.date_num }}</div>
                </div>
            </div>
        </div>

        <!-- Row 2, Col 2: Usage by Model -->
        <div class="card">
            <div class="card-title">Usage by Model</div>
            <div v-if="models.length === 0" class="dim" style="padding:20px 0;text-align:center">No data</div>
            <div v-for="model in models" :key="model.name" class="agent-row">
                <span class="badge" :style="badgeStyle(model.color)">{{ model.short }}</span>
                <div class="agent-bar-wrap">
                    <div class="agent-bar" :style="{ width: model.bar_pct + '%', background: model.color + '40' }"></div>
                </div>
                <div class="agent-total-wrap">
                    <span class="agent-total">{{ fmt(model.total) }}</span>
                    <span class="agent-cost" v-if="model.cost > 0">{{ fmtCost(model.cost) }}</span>
                </div>
            </div>
        </div>

    </div>

    <!-- Sessions Table -->
    <div class="table-card">
        <div class="table-header">
            <div class="card-title" style="margin-bottom:0">Sessions</div>
        </div>
        <div class="table-scroll">
            <table>
                <thead>
                    <tr>
                        <th>Session</th>
                        <th>Agent</th>
                        <th>Model</th>
                        <th>Date</th>
                        <th style="text-align:right">Input</th>
                        <th style="text-align:right">Output</th>
                        <th style="text-align:right">Cache Write</th>
                        <th style="text-align:right">Cache Read</th>
                        <th style="text-align:right">Total</th>
                        <th style="text-align:right">Est. Cost</th>
                    </tr>
                </thead>
                <tbody>
                    <tr v-for="session in sessions" :key="session.id" :class="{ today: session.is_today }">
                        <td class="mono">{{ session.id }}</td>
                        <td><span class="badge" :style="badgeStyle(session.agent_color)">{{ displayName(session.agent) }}</span></td>
                        <td><span class="badge" :style="badgeStyle(session.model_color)">{{ session.model_short }}</span></td>
                        <td>{{ session.date }}</td>
                        <td class="num">{{ fmt(session.input) }}</td>
                        <td class="num">{{ fmt(session.output) }}</td>
                        <td class="num">{{ fmt(session.cache_write) }}</td>
                        <td class="num">{{ fmt(session.cache_read) }}</td>
                        <td class="num bold">{{ fmt(session.total) }}</td>
                        <td class="num cost-cell">{{ fmtCost(session.cost) }}</td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>

</div>

<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
<script>
const SERVE_MODE = __SERVE_MODE__;

Vue.createApp({
    data() {
        return {
            ...(__INITIAL_DATA__),
            serveMode: SERVE_MODE,
            refreshing: false,
            lineTab: 'model',
            hoverIdx: null,
            healthNow: Date.now(),
            settingsOpen: false,
            agentDraft: [],
        };
    },

    computed: {
        lineMaxY() {
            if (!this.line || !this.line.buckets) return 0;
            const tab = this.lineTab;
            let max = 0;
            for (const b of this.line.buckets) {
                const src = tab === 'model' ? b.by_model : b.by_agent;
                for (const v of Object.values(src)) {
                    if (v > max) max = v;
                }
            }
            return max;
        },

        linePaths() {
            if (!this.line || !this.line.buckets || this.lineMaxY === 0) return [];
            const W = 1000, PLOT_H = 120, PAD_TOP = 10;
            const buckets = this.line.buckets;
            const n = buckets.length;
            const maxY = this.lineMaxY;
            const tab = this.lineTab;
            const bottomY = PAD_TOP + PLOT_H;

            const series = tab === 'model'
                ? this.line.models.map(m => ({ key: m, label: this.modelShort(m), color: this.modelColor(m) }))
                : this.line.agents.map(a => ({ key: a, label: this.displayName(a), color: this.agentColor(a) }));

            return series.map(s => {
                const coords = buckets.map((b, i) => {
                    const src = tab === 'model' ? b.by_model : b.by_agent;
                    const v = src[s.key] || 0;
                    const x = n > 1 ? (i / (n - 1)) * W : W / 2;
                    const y = PAD_TOP + PLOT_H * (1 - v / maxY);
                    return [+x.toFixed(1), +y.toFixed(1)];
                });
                const points = coords.map(c => c.join(',')).join(' ');
                const fillPoints = coords[0][0] + ',' + bottomY + ' '
                    + points + ' '
                    + coords[coords.length - 1][0] + ',' + bottomY;
                return { ...s, points, fillPoints };
            });
        },

        lineXLabels() {
            if (!this.line || !this.line.buckets) return [];
            const buckets = this.line.buckets;
            const n = buckets.length;
            const W = 1000;
            const step = 12; // one label per hour (12 × 5min)
            const labels = [];
            for (let i = 0; i < n; i += step) {
                const x = n > 1 ? (i / (n - 1)) * W : W / 2;
                labels.push({ x: x.toFixed(1), text: buckets[i].ts });
            }
            return labels;
        },

        hoverVBX() {
            if (this.hoverIdx === null || !this.line) return 0;
            const n = this.line.buckets.length;
            return n > 1 ? (this.hoverIdx / (n - 1)) * 1000 : 500;
        },

        hoverBucket() {
            if (this.hoverIdx === null || !this.line) return null;
            return this.line.buckets[this.hoverIdx];
        },

        hoverValues() {
            if (!this.hoverBucket) return [];
            const b = this.hoverBucket;
            const tab = this.lineTab;
            return this.linePaths.map(s => {
                const src = tab === 'model' ? b.by_model : b.by_agent;
                const costSrc = tab === 'model' ? b.cost_by_model : b.cost_by_agent;
                return { label: s.label, color: s.color, value: src[s.key] || 0, cost: (costSrc && costSrc[s.key]) || 0 };
            });
        },

        tooltipStyle() {
            if (this.hoverIdx === null || !this.line) return { display: 'none' };
            const n = this.line.buckets.length;
            const xPct = n > 1 ? (this.hoverIdx / (n - 1)) * 100 : 50;
            const flip = this.hoverIdx > n * 0.65;
            return {
                display: 'block',
                position: 'absolute',
                bottom: 'calc(100% - 10px)',
                ...(flip
                    ? { right: (100 - xPct).toFixed(1) + '%', left: 'auto', transform: 'translateX(50%)' }
                    : { left: xPct.toFixed(1) + '%', right: 'auto', transform: 'translateX(-50%)' }),
            };
        },
    },

    methods: {
        fmt(n) {
            return (n || 0).toLocaleString();
        },
        fmtCost(n) {
            if (!n || n < 0.01) return '< $0.01';
            if (n < 1000) return '$' + n.toFixed(2);
            return '$' + n.toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        },
        badgeStyle(color) {
            return { background: color + '18', color };
        },
        modelShort(name) {
            if (!name) return '\u2014';
            const parts = name.replace('claude-', '').split('-');
            if (parts.length >= 3 && !isNaN(parseInt(parts[1]))) {
                return parts[0].charAt(0).toUpperCase() + parts[0].slice(1) + ' ' + parts[1] + '.' + parts[2];
            }
            return parts[0].charAt(0).toUpperCase() + parts[0].slice(1);
        },
        modelColor(name) {
            if (!name) return '#8a8a9a';
            if (name.includes('opus'))   return '#a855f7';
            if (name.includes('sonnet')) return '#6c9bff';
            if (name.includes('haiku'))  return '#7ee8a0';
            return '#8a8a9a';
        },
        agentColor(name) {
            return (this.agent_colors && this.agent_colors[name]) || '#8a8a9a';
        },
        getHoverY(key) {
            if (this.hoverIdx === null || !this.line) return 0;
            const PLOT_H = 120, PAD_TOP = 10;
            const b = this.line.buckets[this.hoverIdx];
            const src = this.lineTab === 'model' ? b.by_model : b.by_agent;
            const v = src[key] || 0;
            const maxY = this.lineMaxY || 1;
            return PAD_TOP + PLOT_H * (1 - v / maxY);
        },
        onLineHover(event) {
            const svg = event.currentTarget;
            const rect = svg.getBoundingClientRect();
            const relX = (event.clientX - rect.left) / rect.width;
            const n = this.line && this.line.buckets ? this.line.buckets.length : 0;
            if (n === 0) return;
            this.hoverIdx = Math.max(0, Math.min(n - 1, Math.round(relX * (n - 1))));
        },
        onLineLeave() {
            this.hoverIdx = null;
        },
        displayName(raw) {
            if (!this.all_agents) return raw;
            const a = this.all_agents.find(x => x.name === raw);
            return (a && a.display_name) ? a.display_name : raw;
        },
        openSettings() {
            this.agentDraft = JSON.parse(JSON.stringify(this.all_agents || []));
            this.settingsOpen = true;
        },
        closeSettings() {
            this.settingsOpen = false;
        },
        autoSave(delay) {
            if (!this.serveMode) return;
            if (this._saveTimer) clearTimeout(this._saveTimer);
            this._saveTimer = setTimeout(async () => {
                const agentsConfig = {};
                for (const a of this.agentDraft) {
                    agentsConfig[a.name] = { visible: a.visible, display_name: a.display_name || '' };
                }
                try {
                    const res = await fetch('/api/config', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ agents: agentsConfig }),
                    });
                    if (res.ok) await this.refresh();
                } catch (e) {
                    console.warn('Auto-save failed:', e);
                }
            }, delay || 0);
        },
        ctxBarColor(pct) {
            if (pct < 60) return '#7ee8a0';
            if (pct < 85) return '#ffb86c';
            return '#ff6cab';
        },
        fmtCtx(n) {
            if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
            if (n >= 1000) return Math.round(n / 1000) + 'K';
            return String(n);
        },
        fmtAgo(ts) {
            if (!ts) return '\u2014';
            const secs = Math.floor((this.healthNow - ts * 1000) / 1000);
            if (secs < 0)   return 'just now';
            if (secs < 60)  return secs + 's ago';
            if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
            if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
            return Math.floor(secs / 86400) + 'd ago';
        },
        async refreshHealth() {
            try {
                const res = await fetch('/api/health');
                if (res.ok) {
                    const data = await res.json();
                    this.agent_health = data.agents;
                }
            } catch (e) {
                console.warn('Health poll failed:', e);
            }
        },
        async refresh() {
            if (this.refreshing) return;
            this.refreshing = true;
            try {
                const res = await fetch('/api/data');
                if (res.ok) {
                    const data = await res.json();
                    Object.assign(this.$data, data);
                }
            } catch (e) {
                console.warn('Dashboard refresh failed:', e);
            } finally {
                this.refreshing = false;
            }
        },
    },

    mounted() {
        setInterval(() => { this.healthNow = Date.now(); }, 1000);
        if (SERVE_MODE) {
            setInterval(this.refreshHealth, 5 * 1000);
            setInterval(this.refresh, 5 * 60 * 1000);
        }
    },
}).mount('#app');
</script>
</body>
</html>'''


def generate_html(data, serve_mode=False):
    """Render the Vue-powered dashboard HTML with baked-in initial data."""
    safe_json = json.dumps(data).replace("</", "<\\/")
    html = _HTML_TEMPLATE
    html = html.replace("__SERVE_MODE__", "true" if serve_mode else "false")
    html = html.replace("__INITIAL_DATA__", safe_json)
    return html


# ---------------------------------------------------------------------------
# Server mode
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler serving the live dashboard and a JSON data API."""

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            config = load_config()
            sessions = collect_all_data()
            data = compute_dashboard_data(sessions, config=config)
            html = generate_html(data, serve_mode=True)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        elif self.path == "/api/data":
            config = load_config()
            sessions = collect_all_data()
            data = compute_dashboard_data(sessions, config=config)
            payload = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)

        elif self.path == "/api/health":
            payload = json.dumps({
                "agents": collect_agent_health(),
                "server_time": datetime.now().timestamp(),
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                config = json.loads(body)
                save_config(config)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except (json.JSONDecodeError, OSError, ValueError):
                self.send_response(400)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def serve(port=8080):
    """Start a local HTTP server that serves a live, auto-refreshing dashboard."""
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    url = f"http://localhost:{port}"
    print(f"Dashboard running at {url}")
    print("Auto-refreshes every 5 minutes. Press Ctrl+C to stop.\n")

    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Claude Code Token Dashboard")
    parser.add_argument("--serve", action="store_true", help="Start a live dashboard server")
    parser.add_argument("--port", type=int, default=8080, help="Server port (default: 8080)")
    parser.add_argument("--output", "-o", default="token_dashboard.html", help="Output HTML file path")
    args = parser.parse_args()

    if args.serve:
        serve(args.port)
    else:
        print("Scanning sessions...")
        sessions = collect_all_data()
        print(f"Found {len(sessions)} sessions")

        data = compute_dashboard_data(sessions, config=load_config())
        html = generate_html(data, serve_mode=False)

        output = os.path.abspath(args.output)
        with open(output, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Dashboard written to {output}")


if __name__ == "__main__":
    main()
