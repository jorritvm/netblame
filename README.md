# netblame

**Whose fault is it?** netblame pings every hop of your network path *in parallel*:
your LAN router, the ISP gateway and one or more internet hosts. After a
configurable window (default 2 minutes) it prints a console report that tells
you **which segment is to blame** for packet loss and latency.

```
Verdict
-------
  !! Problem located in the ISP line / upstream network (between Telenet HGW and
     Google DNS / Cloudflare DNS).

  Evidence:
    - Google DNS (8.8.8.8): 16.7% loss, p50 109 ms, p95 1459 ms, max 2683 ms, jitter 372 ms
    - Closer hops over the same period: ER605 0.0% loss / p95 5.6 ms, Telenet HGW 0.0% loss / p95 7.7 ms.
    - 8 of 8 bad moments at this hop happened while all closer hops were healthy.
```

Because all targets are pinged at the same moments, netblame can tell a
problem that *starts* at a hop apart from one that is just inherited from a
closer hop. The analysis is deterministic Python. An LLM explanation is
optional (`--explain`) and never changes the verdict.

Windows only (it uses the built-in `ping` command, so no admin rights are needed).

## Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). netblame is
installed into a local `.venv` inside the project folder, so nothing touches
your global Python.

```powershell
cd C:\dev\python\netblame

# create .venv and install netblame into it (editable: code changes apply immediately)
uv sync

# or include the LLM extra for --explain
uv sync --extra llm
```

`uv sync` also writes a `uv.lock` with the exact dependency versions. Commit
it to git, so a fresh clone gets the same environment.

Run it from the project folder, either through uv (no activation needed):

```powershell
uv run netblame -d 5m
uv run --extra llm netblame --explain
```

or by activating the venv first:

```powershell
.venv\Scripts\activate
netblame -d 5m
deactivate
```

You can also call it directly from anywhere without activating:
`C:\dev\python\netblame\.venv\Scripts\netblame.exe`.

To remove it, delete the `.venv` folder.

## Usage

```powershell
netblame                      # run with config/defaults (2 minutes)
netblame -d 5m                # measure for 5 minutes (90, 90s, 2m, 1m30s, 1h ...)
netblame -d 15m -i 0.5        # longer window, 2 pings per second per target
netblame --show-config        # print the effective settings and exit
netblame --explain            # add a plain-language explanation from Claude
```

Custom targets on the command line, in path order. They replace the configured ones:

```powershell
netblame -t ER605=192.168.1.1:lan -t HGW=192.168.0.1:isp_gateway -t Google=8.8.8.8
```

`KIND` is `lan`, `isp_gateway` or `internet` (default `internet`).

Press **Ctrl+C** to stop early; netblame analyses what it has collected so far.

| Option | Meaning |
|---|---|
| `-c, --config PATH` | YAML config file |
| `-d, --duration TIME` | measurement window (default `2m`, 10s–24h) |
| `-i, --interval SEC` | seconds between pings per target (default 1) |
| `-w, --timeout MS` | reply timeout per ping (default 1000) |
| `-t, --target NAME=HOST[:KIND]` | target, repeatable, path order |
| `--explain` | LLM explanation (needs `netblame[llm]` + `ANTHROPIC_API_KEY`) |
| `--model NAME` | model for `--explain` |
| `--no-progress` | hide the live progress line |
| `--show-config` | print effective settings and exit |

**Exit codes:** `0` healthy · `1` problem found · `2` error · `130` stopped before any data.
That makes it easy to script, e.g. a scheduled task that logs only failing runs.

## Configuration

netblame looks for a config file in this order:

1. `--config PATH`
2. the `NETBLAME_CONFIG` environment variable
3. `config\netblame.yaml` in the current working directory
4. built-in defaults

See [`config/netblame.yaml`](config/netblame.yaml) for all keys with comments:
run timing, targets and thresholds per kind. Every key is optional. To use your
config from anywhere, point the environment variable at it:

```powershell
setx NETBLAME_CONFIG C:\dev\python\netblame\config\netblame.yaml
```

Tip: configure **two internet targets** (e.g. 8.8.8.8 and 1.1.1.1). If only one
is bad, netblame reports a destination-specific problem instead of blaming your line.

## Reading the report

- **Per target**: loss, median/p95/max round-trip time and jitter, graded OK / WARN / BAD.
- **Timeline**: one row per target on a shared time axis
  (`.` ok, `~` slow, `x` some lost, `X` half or more lost). Columns line up in
  time, so you can see at a glance whether trouble starts at a closer hop.
- **Worst incidents**: streaks of consecutive bad samples, with what the closer
  hops looked like at that moment.
- **Verdict**: the blamed segment, the evidence, and what to check next.

How the verdict is reached, and why the thresholds are what they are, is
explained in [docs/analysis.md](docs/analysis.md).

## Project layout

```
netblame/
├── config/netblame.yaml     default config (targets, timing, thresholds)
├── docs/analysis.md         analysis method and thresholds
├── src/netblame/
│   ├── cli.py               argument parsing, run orchestration
│   ├── config.py            settings, YAML loading, validation
│   ├── pinger.py            parallel ping sampling (Windows ping via subprocess)
│   ├── analysis.py          statistics, hop correlation, verdict
│   ├── report.py            console report
│   └── explain.py           optional LLM explanation
├── pyproject.toml
└── LICENSE                  MIT
```

## License

MIT, see [LICENSE](LICENSE).
