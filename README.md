# Claude Code Token Dashboard

A local dashboard that parses your Claude Code session transcripts and shows token usage statistics. No cloud, no API keys — reads the JSONL files Claude Code already writes to your machine.

<table>
  <tr>
    <td><img src="screenshots/light.png" alt="Light mode" /></td>
    <td><img src="screenshots/dark-settings.png" alt="Dark mode with settings" /></td>
  </tr>
</table>

## Features

- **Today / Week / All-Time stats** at a glance
- **Activity line graph** — last 5 hours at 5-minute resolution, switchable between model and agent views, Y-axis labels, hover tooltips
- **7-day bar chart** showing daily token usage trends
- **Per-agent breakdown** — today's usage split by agent/project
- **Usage by model** — all-time token breakdown across Opus, Sonnet, and Haiku
- **Session-level detail** — input, output, cache write, and cache read tokens per session
- **Agent Health Monitor** — tmux session status, last-active counter, and context window usage; only shows sessions backed by Claude Code transcripts
- **Activity feed** — real-time log of agent tool calls and messages parsed from JSONL transcripts; filterable by agent and type; polls every 8 seconds in server mode
- **Settings panel** — toggle agent visibility, set display names; changes apply instantly in server mode
- **Light / dark mode** — toggle with one click, preference saved to localStorage
- **Reactive live server** — Vue 3 powered, polls `/api/data` every 5 minutes and updates in place without a page reload
- **Zero Python dependencies** — standard library only, no pip installs

## Requirements

- Python 3.7+
- Claude Code (the CLI) — reads session files from `~/.claude/projects/`

No pip packages needed. Standard library only.

## Usage

### Live Server (recommended)

```bash
python3 dashboard.py --serve
```

Opens a dashboard at `http://localhost:8080`. The page auto-refreshes every 5 minutes — data updates reactively without reloading. A pulsing indicator and manual refresh button (↻) are shown in the header. Press Ctrl+C to stop.

### Static HTML

```bash
python3 dashboard.py
open token_dashboard.html
```

Generates a self-contained HTML snapshot. Vue still renders the initial data reactively, but polling is disabled since there's no server to query.

### Options

```
python3 dashboard.py                  # Generate static HTML
python3 dashboard.py --serve          # Start live server (default port 8080)
python3 dashboard.py --port 3000      # Custom port
python3 dashboard.py -o report.html   # Custom output path
```

## Layout

The dashboard has two top-level tabs: **Tokens** and **Activity**.

### Tokens tab

```
[ Today ] [ This Week ] [ All Time ]

[ Activity — Last 5 hrs  ] [ Today by Agent  ]
[ Last 7 Days bar chart   ] [ Usage by Model  ]

[ Sessions table ]
```

The activity line graph shows token throughput in 5-minute buckets. Toggle between **Model** (Opus / Sonnet / Haiku) and **Agent** views using the tab in the card header. Y-axis labels show the max and midpoint values. Hover over the graph to see a tooltip with the exact time and token count for each series at that point.

### Activity tab

A live feed of agent tool calls and messages parsed from JSONL transcripts. Each entry shows the agent name, action type (tool call, message, agent-to-agent comms), a short summary, and a timestamp. Filter by agent or event type using the toolbar dropdowns. In server mode, the feed refreshes every 8 seconds and new entries animate in.

## How It Works

Claude Code stores session transcripts as JSONL files in `~/.claude/projects/`. Each line contains message data including token usage. This tool:

1. Scans all `.jsonl` files recursively
2. Extracts input, output, and cache tokens from each message
3. Groups them by session and infers the agent/project from the directory path
4. Serves data as JSON via `/api/data`, rendered by a Vue 3 reactive frontend

Agent names are auto-detected from the directory structure — no configuration needed.

**There is no pre-built HTML file in this repo.** The entire dashboard template is embedded inside `dashboard.py`. Running the script generates `token_dashboard.html` (static mode) or serves it live (server mode) with your own data filled in.

No data leaves your machine.

## Contributing

PRs welcome. Keep it simple — stdlib only, single Python file, self-contained HTML output.

## License

MIT
