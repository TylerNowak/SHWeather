# Contributing

Thanks for helping sailors get better weather.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
shweather serve --demo      # http://localhost:8080
```

## Ground rules

- **Canonical units everywhere inside the server**: knots, degrees true, hPa (MSL),
  metres, seconds, °C, Unix seconds UTC. Convert at the edges only (providers in, PWA out).
- **Every endpoint must work offline.** API handlers read local state; only `refresh_*`
  methods touch the network.
- **A new data source** is a module in `server/shweather/providers/` that returns
  canonical series or records, raises `NotApplicable` outside its coverage and
  `ProviderError` on bad data, and ships with a fixture-based test. Add a matching
  responder to `demo.py` so demo mode exercises it.
- **Safety first in wording.** Never overstate accuracy; label heuristics as heuristics.
- **No build step for `web/`.** Plain ES modules; untrusted text goes in via
  `textContent`, never `innerHTML`.
- **Units:** never format a number with a unit in the web app by hand; go through
  `web/js/units.js` (and `text.js` for sentences) so Metric / Imperial / Nautical all work.
- **Windows:** keep `deploy/windows/*.ps1` ASCII-only (Windows PowerShell 5.1 reads them
  as ANSI) and don't use POSIX-only Python modules.

## Before a pull request

```bash
pytest                    # includes the Windows script checks when pwsh is installed
ruff check server tests
npm test                  # web unit-system tests (Node 20+)
```

Recorded real API responses make the best fixtures. Please strip anything personal.
