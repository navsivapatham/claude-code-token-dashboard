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
import webbrowser
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

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
# Line graph data (last N hours, 5-min buckets)
# ---------------------------------------------------------------------------

def compute_line_data(sessions, hours=5, bucket_minutes=5):
    """Return per-5-minute token usage for the last N hours, grouped by model and agent."""
    now = datetime.now().astimezone()
    cutoff = now - timedelta(hours=hours)
    n_buckets = (hours * 60) // bucket_minutes

    buckets_model = [defaultdict(int) for _ in range(n_buckets)]
    buckets_agent = [defaultdict(int) for _ in range(n_buckets)]
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
            total = entry["input_tokens"] + entry["output_tokens"] + entry["cache_creation_input_tokens"]
            buckets_model[idx][model] += total
            buckets_agent[idx][agent] += total
            all_models.add(model)
            all_agents.add(agent)

    serialized = []
    for i in range(n_buckets):
        serialized.append({
            "ts": bucket_labels[i],
            "by_model": dict(buckets_model[i]),
            "by_agent": dict(buckets_agent[i]),
        })

    return {
        "buckets": serialized,
        "models": sorted(all_models),
        "agents": sorted(all_agents),
    }


# ---------------------------------------------------------------------------
# Dashboard data computation
# ---------------------------------------------------------------------------

def compute_dashboard_data(sessions):
    """Compute all dashboard data and return as a JSON-serializable dict."""
    now = datetime.now().astimezone()
    today = now.date()

    sorted_sessions_list = sorted(
        sessions.items(),
        key=lambda x: x[1]["first_timestamp"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    token_keys = ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"]
    daily = defaultdict(lambda: dict.fromkeys(token_keys, 0))
    agent_today = defaultdict(lambda: dict.fromkeys(token_keys, 0))

    for sid, s in sorted_sessions_list:
        for entry in s.get("entries", []):
            et = entry.get("parsed_timestamp") or s["first_timestamp"]
            if et is None:
                continue
            day = et.astimezone().date()
            for k in token_keys:
                daily[day][k] += entry[k]
            if day == today:
                agent = entry.get("agent", s["agent"])
                for k in token_keys:
                    agent_today[agent][k] += entry[k]

    # Chart — last 7 days
    chart = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        t = daily.get(d, dict.fromkeys(token_keys, 0))
        total = t["input_tokens"] + t["output_tokens"] + t["cache_creation_input_tokens"]
        chart.append({"label": d.strftime("%a"), "date_num": d.strftime("%d"), "total": total, "is_today": d == today})

    max_daily = max((d["total"] for d in chart), default=1) or 1
    for d in chart:
        d["bar_pct"] = max(round((d["total"] / max_daily) * 100, 1), 2)

    # Today stats
    td = daily.get(today, dict.fromkeys(token_keys, 0))
    today_total = td["input_tokens"] + td["output_tokens"] + td["cache_creation_input_tokens"]

    # All-time
    all_time = dict.fromkeys(token_keys, 0)
    for dv in daily.values():
        for k in token_keys:
            all_time[k] += dv[k]
    all_time_total = all_time["input_tokens"] + all_time["output_tokens"] + all_time["cache_creation_input_tokens"]

    # Agent colors
    all_agents = sorted(set(s["agent"] for _, s in sorted_sessions_list))
    agent_colors = {agent: AGENT_PALETTE[i % len(AGENT_PALETTE)] for i, agent in enumerate(all_agents)}

    # Agents today
    agents_today_data = []
    for agent in all_agents:
        a = agent_today.get(agent, dict.fromkeys(token_keys, 0))
        total = a["input_tokens"] + a["output_tokens"] + a["cache_creation_input_tokens"]
        agents_today_data.append({
            "name": agent,
            "total": total,
            "bar_pct": round(min((total / max(today_total, 1)) * 100, 100), 1),
            "color": agent_colors.get(agent, "#8a8a9a"),
        })

    # Models
    model_totals = defaultdict(lambda: dict.fromkeys(["input_tokens", "output_tokens", "cache_creation_input_tokens"], 0))
    for sid, s in sorted_sessions_list:
        for entry in s.get("entries", []):
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
            "bar_pct": round(min((total / max_model) * 100, 100), 1),
            "color": _model_color(model_name),
        })

    # Sessions
    sessions_data = []
    for sid, s in sorted_sessions_list:
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
            "is_today": day == today if day else False,
        })

    return {
        "stats": {
            "today_total": today_total,
            "today_input": td["input_tokens"],
            "today_output": td["output_tokens"],
            "today_cache": td["cache_creation_input_tokens"],
            "week_total": sum(d["total"] for d in chart),
            "all_time_total": all_time_total,
            "session_count": len(sorted_sessions_list),
        },
        "chart": chart,
        "agents_today": agents_today_data,
        "models": models_data,
        "sessions": sessions_data,
        "updated": now.strftime("%b %d, %Y at %H:%M"),
        "agent_colors": agent_colors,
        "line": compute_line_data(sessions),
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
    .stat-label {
        color: var(--text-secondary);
        font-size: 0.7rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 8px;
    }
    .stat-value {
        font-size: 2rem;
        font-weight: 800;
        color: var(--text-primary);
        letter-spacing: -0.02em;
    }
    .stat-detail { color: var(--text-secondary); font-size: 0.72rem; margin-top: 6px; }
    .stat-detail span { margin-right: 12px; }

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
    .agent-total {
        font-weight: 700;
        color: var(--text-primary);
        font-size: 0.85rem;
        min-width: 80px;
        text-align: right;
        font-family: 'SF Mono', 'Fira Code', monospace;
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

    @media (max-width: 768px) {
        .stats-grid { grid-template-columns: 1fr; }
        .main-grid { grid-template-columns: 1fr; }
        .stat-value { font-size: 1.5rem; }
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
        </div>
    </div>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-label">Today</div>
            <div class="stat-value">{{ fmt(stats.today_total) }}</div>
            <div class="stat-detail">
                <span>In: {{ fmt(stats.today_input) }}</span>
                <span>Out: {{ fmt(stats.today_output) }}</span>
                <span>Cache: {{ fmt(stats.today_cache) }}</span>
            </div>
        </div>
        <div class="stat-card">
            <div class="stat-label">This Week</div>
            <div class="stat-value">{{ fmt(stats.week_total) }}</div>
            <div class="stat-detail">Last 7 days</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">All Time</div>
            <div class="stat-value">{{ fmt(stats.all_time_total) }}</div>
            <div class="stat-detail">{{ stats.session_count }} sessions</div>
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
                <span class="badge" :style="badgeStyle(agent.color)">{{ agent.name }}</span>
                <template v-if="agent.total > 0">
                    <div class="agent-bar-wrap">
                        <div class="agent-bar" :style="{ width: agent.bar_pct + '%', background: agent.color + '40' }"></div>
                    </div>
                    <span class="agent-total">{{ fmt(agent.total) }}</span>
                </template>
                <template v-else>
                    <span class="agent-total dim">\u2014</span>
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
                <span class="agent-total">{{ fmt(model.total) }}</span>
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
                    </tr>
                </thead>
                <tbody>
                    <tr v-for="session in sessions" :key="session.id" :class="{ today: session.is_today }">
                        <td class="mono">{{ session.id }}</td>
                        <td><span class="badge" :style="badgeStyle(session.agent_color)">{{ session.agent }}</span></td>
                        <td><span class="badge" :style="badgeStyle(session.model_color)">{{ session.model_short }}</span></td>
                        <td>{{ session.date }}</td>
                        <td class="num">{{ fmt(session.input) }}</td>
                        <td class="num">{{ fmt(session.output) }}</td>
                        <td class="num">{{ fmt(session.cache_write) }}</td>
                        <td class="num">{{ fmt(session.cache_read) }}</td>
                        <td class="num bold">{{ fmt(session.total) }}</td>
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
                : this.line.agents.map(a => ({ key: a, label: a, color: this.agentColor(a) }));

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
                return { label: s.label, color: s.color, value: src[s.key] || 0 };
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
        if (SERVE_MODE) {
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
            sessions = collect_all_data()
            data = compute_dashboard_data(sessions)
            html = generate_html(data, serve_mode=True)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        elif self.path == "/api/data":
            sessions = collect_all_data()
            data = compute_dashboard_data(sessions)
            payload = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)

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

        data = compute_dashboard_data(sessions)
        html = generate_html(data, serve_mode=False)

        output = os.path.abspath(args.output)
        with open(output, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Dashboard written to {output}")


if __name__ == "__main__":
    main()
