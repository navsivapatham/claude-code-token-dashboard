# Claude Code Token Dashboard

A local dashboard that parses your Claude Code session transcripts and shows token usage statistics. No cloud, no API keys — reads the JSONL files Claude Code already writes to your machine.

![Dashboard Screenshot](screenshot.png)

## Features

- **Today / Week / All-Time stats** at a glance
- **7-day bar chart** showing daily token usage trends
- **Per-agent breakdown** if you run multiple Claude Code projects
- **Session-level detail** with input, output, cache write, and cache read tokens
- **Live server mode** — auto-refreshes data on each page load
- **Self-contained HTML** — zero external dependencies, works offline

## Requirements

- Python 3.7+
- Claude Code (the CLI) — this reads its local session files from `~/.claude/projects/`

No pip packages needed. Standard library only.

## Usage

### Static HTML (generate and open)

```bash
python3 dashboard.py
open token_dashboard.html
```

### Live Server (recommended)

```bash
python3 dashboard.py --serve
```

Opens a dashboard at `http://localhost:8080` that regenerates data on every page load. Press Ctrl+C to stop.

### Options

```
python3 dashboard.py                  # Generate static HTML
python3 dashboard.py --serve          # Start live server
python3 dashboard.py --port 3000      # Custom port
python3 dashboard.py -o report.html   # Custom output path
```

## How It Works

Claude Code stores session transcripts as JSONL files in `~/.claude/projects/`. Each line contains message data including token usage. This tool:

1. Scans all `.jsonl` files recursively
2. Extracts input, output, and cache tokens from each message
3. Groups them by session and infers the agent/project from the directory path
4. Generates a self-contained HTML dashboard with all CSS inline

No data leaves your machine. The dashboard is a static HTML file you can open in any browser.

## Contributing

PRs welcome. Keep it simple — stdlib only, single file, self-contained HTML output.

## License

MIT
