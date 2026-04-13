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
import sys
import glob
import argparse
import webbrowser
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


# ---------------------------------------------------------------------------
# Data parsing
# ---------------------------------------------------------------------------

def detect_agent(filepath):
    """Detect agent name from the project directory path.

    Claude Code stores sessions under ~/.claude/projects/<encoded-path>/.
    The encoded path uses dashes instead of slashes, so a project at
    ~/Work/agents/athena becomes -Users-name-Work-agents-athena.

    We extract the last meaningful directory segment as the agent/project name.
    """
    parts = Path(filepath).parts
    for part in parts:
        if part.startswith("-") and "-" in part[1:]:
            # This is an encoded project path — take the last segment
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
# HTML generation
# ---------------------------------------------------------------------------

def fmt(n):
    """Format number with commas."""
    return f"{n:,}"


def generate_html(sessions):
    """Generate a self-contained HTML dashboard."""
    now = datetime.now().astimezone()
    today = now.date()

    sorted_sessions = sorted(
        sessions.items(),
        key=lambda x: x[1]["first_timestamp"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    # Aggregate daily totals
    daily = defaultdict(lambda: {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0})
    agent_today = defaultdict(lambda: {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0})

    for sid, s in sorted_sessions:
        for entry in s.get("entries", []):
            et = entry.get("parsed_timestamp") or s["first_timestamp"]
            if et is None:
                continue
            day = et.astimezone().date()
            daily[day]["input_tokens"] += entry["input_tokens"]
            daily[day]["cache_creation_input_tokens"] += entry["cache_creation_input_tokens"]
            daily[day]["cache_read_input_tokens"] += entry["cache_read_input_tokens"]
            daily[day]["output_tokens"] += entry["output_tokens"]

            if day == today:
                agent = entry.get("agent", s["agent"])
                agent_today[agent]["input_tokens"] += entry["input_tokens"]
                agent_today[agent]["cache_creation_input_tokens"] += entry["cache_creation_input_tokens"]
                agent_today[agent]["cache_read_input_tokens"] += entry["cache_read_input_tokens"]
                agent_today[agent]["output_tokens"] += entry["output_tokens"]

    # Last 7 days
    days_list = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        totals = daily.get(d, {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0})
        total = totals["input_tokens"] + totals["output_tokens"] + totals["cache_creation_input_tokens"]
        days_list.append({"date": d, "total": total, **totals})

    max_daily = max((d["total"] for d in days_list), default=1) or 1

    today_totals = daily.get(today, {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0})
    today_total = today_totals["input_tokens"] + today_totals["output_tokens"] + today_totals["cache_creation_input_tokens"]

    # All-time totals
    all_time = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    for d in daily.values():
        for k in all_time:
            all_time[k] += d[k]
    all_time_total = all_time["input_tokens"] + all_time["output_tokens"] + all_time["cache_creation_input_tokens"]

    # Discover all agents
    all_agents = sorted(set(s["agent"] for _, s in sorted_sessions))

    # Agent color palette
    palette = ["#6c9bff", "#ff6cab", "#ffb86c", "#7ee8a0", "#c4a0ff", "#ff8a80", "#80d8ff", "#ffd54f"]
    agent_colors = {}
    for i, agent in enumerate(all_agents):
        agent_colors[agent] = palette[i % len(palette)]

    # Build agent CSS
    agent_css = ""
    for agent, color in agent_colors.items():
        safe = agent.replace(" ", "-").replace("/", "-")
        agent_css += f"    .agent-{safe} {{ background: {color}18; color: {color}; }}\n"

    # Session rows
    session_rows = ""
    for sid, s in sorted_sessions:
        total = s["input_tokens"] + s["output_tokens"] + s["cache_creation_input_tokens"]
        ts = s["first_timestamp"]
        ts_local = ts.astimezone() if ts else None
        date_str = ts_local.strftime("%b %d, %H:%M") if ts_local else "—"
        day = ts_local.date() if ts_local else None
        is_today = ' class="today"' if day == today else ""
        safe_agent = s["agent"].replace(" ", "-").replace("/", "-")
        session_rows += f"""<tr{is_today}>
            <td class="mono">{sid[:12]}</td>
            <td><span class="badge agent-{safe_agent}">{s['agent']}</span></td>
            <td>{date_str}</td>
            <td class="num">{fmt(s['input_tokens'])}</td>
            <td class="num">{fmt(s['output_tokens'])}</td>
            <td class="num">{fmt(s['cache_creation_input_tokens'])}</td>
            <td class="num">{fmt(s['cache_read_input_tokens'])}</td>
            <td class="num bold">{fmt(total)}</td>
        </tr>"""

    # Chart bars
    chart_bars = ""
    for d in days_list:
        pct = (d["total"] / max_daily) * 100 if max_daily > 0 else 0
        is_today_class = " bar-today" if d["date"] == today else ""
        label = d["date"].strftime("%a")
        date_num = d["date"].strftime("%d")
        chart_bars += f"""<div class="bar-group">
            <div class="bar-value">{fmt(d['total'])}</div>
            <div class="bar-track"><div class="bar{is_today_class}" style="height: {max(pct, 2):.1f}%"></div></div>
            <div class="bar-day">{label}</div>
            <div class="bar-date">{date_num}</div>
        </div>"""

    # Agent breakdown
    agent_rows = ""
    for agent in all_agents:
        a = agent_today.get(agent, {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0})
        total = a["input_tokens"] + a["output_tokens"] + a["cache_creation_input_tokens"]
        safe = agent.replace(" ", "-").replace("/", "-")
        color = agent_colors.get(agent, "#8a8a9a")
        if total == 0:
            agent_rows += f"""<div class="agent-row">
                <span class="badge agent-{safe}">{agent}</span>
                <span class="agent-total dim">—</span>
            </div>"""
        else:
            bar_w = min((total / max(today_total, 1)) * 100, 100)
            agent_rows += f"""<div class="agent-row">
                <span class="badge agent-{safe}">{agent}</span>
                <div class="agent-bar-wrap">
                    <div class="agent-bar" style="width: {bar_w:.0f}%; background: {color}40;"></div>
                </div>
                <span class="agent-total">{fmt(total)}</span>
            </div>"""

    updated_str = now.strftime("%b %d, %Y at %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code — Token Dashboard</title>
<style>
    :root {{
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
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', system-ui, sans-serif;
        background: var(--bg-primary);
        color: var(--text-primary);
        line-height: 1.5;
        -webkit-font-smoothing: antialiased;
    }}
    .container {{
        max-width: 1200px;
        margin: 0 auto;
        padding: 32px 24px;
    }}

    /* Header */
    .header {{
        margin-bottom: 32px;
    }}
    .header h1 {{
        font-size: 1.6rem;
        font-weight: 700;
        background: linear-gradient(135deg, var(--gradient-start), var(--gradient-end));
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        margin-bottom: 4px;
    }}
    .header .meta {{
        color: var(--text-secondary);
        font-size: 0.8rem;
    }}

    /* Stats Grid */
    .stats-grid {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 16px;
        margin-bottom: 24px;
    }}
    .stat-card {{
        background: var(--bg-card);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 20px;
        transition: border-color 0.2s;
    }}
    .stat-card:hover {{
        border-color: var(--accent);
    }}
    .stat-label {{
        color: var(--text-secondary);
        font-size: 0.7rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 8px;
    }}
    .stat-value {{
        font-size: 2rem;
        font-weight: 800;
        color: var(--text-primary);
        letter-spacing: -0.02em;
    }}
    .stat-detail {{
        color: var(--text-secondary);
        font-size: 0.72rem;
        margin-top: 6px;
    }}
    .stat-detail span {{ margin-right: 12px; }}

    /* Main Grid */
    .main-grid {{
        display: grid;
        grid-template-columns: 2fr 1fr;
        gap: 16px;
        margin-bottom: 24px;
    }}
    .card {{
        background: var(--bg-card);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 20px;
    }}
    .card-title {{
        color: var(--text-secondary);
        font-size: 0.7rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 16px;
    }}

    /* Chart */
    .chart {{
        display: flex;
        align-items: flex-end;
        gap: 6px;
        height: 200px;
    }}
    .bar-group {{
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        height: 100%;
    }}
    .bar-value {{
        font-size: 0.6rem;
        color: var(--text-dim);
        margin-bottom: 6px;
        font-family: 'SF Mono', 'Fira Code', monospace;
        min-height: 14px;
    }}
    .bar-track {{
        flex: 1;
        width: 100%;
        display: flex;
        align-items: flex-end;
    }}
    .bar {{
        width: 100%;
        background: linear-gradient(180deg, var(--gradient-start), var(--gradient-end));
        border-radius: 4px 4px 1px 1px;
        min-height: 2px;
        opacity: 0.5;
        transition: opacity 0.2s;
    }}
    .bar-today {{
        opacity: 1;
        box-shadow: 0 0 20px var(--accent-glow);
    }}
    .bar-group:hover .bar {{ opacity: 0.8; }}
    .bar-day {{
        font-size: 0.7rem;
        color: var(--text-secondary);
        margin-top: 8px;
        font-weight: 500;
    }}
    .bar-date {{
        font-size: 0.65rem;
        color: var(--text-dim);
    }}

    /* Agent Breakdown */
    .agent-row {{
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 0;
        border-bottom: 1px solid var(--border-subtle);
    }}
    .agent-row:last-child {{ border-bottom: none; }}
    .agent-bar-wrap {{
        flex: 1;
        height: 6px;
        background: var(--border-subtle);
        border-radius: 3px;
        overflow: hidden;
    }}
    .agent-bar {{
        height: 100%;
        border-radius: 3px;
        transition: width 0.4s ease;
    }}
    .agent-total {{
        font-weight: 700;
        color: var(--text-primary);
        font-size: 0.85rem;
        min-width: 80px;
        text-align: right;
        font-family: 'SF Mono', 'Fira Code', monospace;
    }}
    .dim {{ color: var(--text-dim); font-weight: 400; }}
    .badge {{
        display: inline-block;
        padding: 3px 10px;
        border-radius: 6px;
        font-size: 0.7rem;
        font-weight: 600;
        min-width: 64px;
        text-align: center;
        white-space: nowrap;
    }}
{agent_css}

    /* Sessions Table */
    .table-card {{
        background: var(--bg-card);
        border: 1px solid var(--border);
        border-radius: 12px;
        overflow: hidden;
    }}
    .table-header {{
        padding: 16px 20px 12px;
    }}
    .table-scroll {{
        max-height: 480px;
        overflow-y: auto;
    }}
    .table-scroll::-webkit-scrollbar {{ width: 5px; }}
    .table-scroll::-webkit-scrollbar-track {{ background: transparent; }}
    .table-scroll::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}
    table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 0.78rem;
    }}
    thead th {{
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
    }}
    tbody td {{
        padding: 10px 14px;
        border-bottom: 1px solid var(--border-subtle);
    }}
    tbody tr {{ transition: background 0.15s; }}
    tbody tr:hover {{ background: var(--bg-card-hover); }}
    tr.today {{ background: rgba(108, 155, 255, 0.04); }}
    tr.today:hover {{ background: rgba(108, 155, 255, 0.08); }}
    .mono {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.73rem; color: var(--text-secondary); }}
    .num {{ text-align: right; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.73rem; }}
    .bold {{ font-weight: 700; color: var(--text-primary); }}

    /* Responsive */
    @media (max-width: 768px) {{
        .stats-grid {{ grid-template-columns: 1fr; }}
        .main-grid {{ grid-template-columns: 1fr; }}
        .stat-value {{ font-size: 1.5rem; }}
    }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>Claude Code Token Dashboard</h1>
        <div class="meta">Updated {updated_str} &middot; {len(sorted_sessions)} sessions</div>
    </div>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-label">Today</div>
            <div class="stat-value">{fmt(today_total)}</div>
            <div class="stat-detail">
                <span>In: {fmt(today_totals['input_tokens'])}</span>
                <span>Out: {fmt(today_totals['output_tokens'])}</span>
                <span>Cache: {fmt(today_totals['cache_creation_input_tokens'])}</span>
            </div>
        </div>
        <div class="stat-card">
            <div class="stat-label">This Week</div>
            <div class="stat-value">{fmt(sum(d['total'] for d in days_list))}</div>
            <div class="stat-detail">Last 7 days</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">All Time</div>
            <div class="stat-value">{fmt(all_time_total)}</div>
            <div class="stat-detail">{len(sorted_sessions)} sessions</div>
        </div>
    </div>

    <div class="main-grid">
        <div class="card">
            <div class="card-title">Last 7 Days</div>
            <div class="chart">
                {chart_bars}
            </div>
        </div>
        <div class="card">
            <div class="card-title">Today by Agent</div>
            {agent_rows if agent_rows else '<div class="dim" style="padding: 20px 0; text-align: center;">No usage today</div>'}
        </div>
    </div>

    <div class="table-card">
        <div class="table-header">
            <div class="card-title" style="margin-bottom: 0;">Sessions</div>
        </div>
        <div class="table-scroll">
            <table>
                <thead>
                    <tr>
                        <th>Session</th>
                        <th>Agent</th>
                        <th>Date</th>
                        <th style="text-align:right">Input</th>
                        <th style="text-align:right">Output</th>
                        <th style="text-align:right">Cache Write</th>
                        <th style="text-align:right">Cache Read</th>
                        <th style="text-align:right">Total</th>
                    </tr>
                </thead>
                <tbody>
                    {session_rows}
                </tbody>
            </table>
        </div>
    </div>
</div>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Server mode
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler that regenerates the dashboard on each request."""

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            sessions = collect_all_data()
            html = generate_html(sessions)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress request logs for cleaner output
        pass


def serve(port=8080):
    """Start a local HTTP server that serves a live dashboard."""
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    url = f"http://localhost:{port}"
    print(f"Dashboard running at {url}")
    print("Press Ctrl+C to stop\n")

    # Open browser
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

        html = generate_html(sessions)

        output = os.path.abspath(args.output)
        with open(output, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Dashboard written to {output}")


if __name__ == "__main__":
    main()
