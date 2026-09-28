# Web app (GitHub Pages)

A mobile-friendly website that runs the **real Python backtest engine in your
browser** via [Pyodide](https://pyodide.org). It calls the same
`build_market_data` / `compute_signals` / `run_*` code as the CLI, so the
numbers match exactly — there is no re-implemented JavaScript math to drift out
of sync.

## How it works

```
docs/
├── index.html      # mobile-first UI (all CLI controls)
├── styles.css      # responsive styling
├── app.js          # boots Pyodide, mounts py+data, calls web_api.run_json
├── manifest.json   # lists which py/data files to mount (written by build_web.py)
├── manifest.webmanifest  # PWA metadata (installable to home screen)
├── py/             # COPY of src/*.py the browser imports
└── data/           # COPY of the NDX / T-bill / S&P CSVs
```

On load, `app.js`:
1. boots Pyodide and loads `pandas` + `numpy`,
2. fetches the `py/*.py` and `data/*.csv` files and writes them into Pyodide's
   in-memory filesystem at `/app/py` and `/app/data`,
3. imports `web_api` and calls `web_api.run_json(configJson)` on every change,
4. renders the results table, equity + signal charts, and the monthly grid.

The browser always runs with `auto_update=False` (it can't fetch via urllib and
CORS), so it uses the bundled data snapshot in `docs/data/`.

## Keeping the site in sync with the engine

The `py/` and `data/` folders are **copies**. After changing anything in `src/`
or `data/`, re-sync them from the project root:

```bash
python build_web.py
```

This copies `data.py`, `strategy.py`, `backtest.py`, `web_api.py` and the three
CSVs into `docs/`, and rewrites `manifest.json`. Commit the result.

To refresh the price data snapshot the site ships, update the CSVs first
(`python src/fetch_data.py --force` etc.), then run `build_web.py`.

## Enable GitHub Pages

1. Push to GitHub (the repo is `specterz/tqqq_161ma`).
2. On GitHub: **Settings → Pages**.
3. Under **Build and deployment → Source**, choose **Deploy from a branch**.
4. Set **Branch** = `main` and **Folder** = `/docs`, then **Save**.
5. Wait ~1 minute; the site publishes at:
   `https://specterz.github.io/tqqq_161ma/`

Any later `git push` to `main` redeploys automatically. Pages is free for public
repositories.

## Local preview

Pyodide needs the files served over HTTP (not opened as `file://`). From the
project root:

```bash
python -m http.server 8000 --directory docs
# then open http://localhost:8000/
```

## Notes & limits

- **First load downloads ~10 MB** (Pyodide runtime + pandas) and takes a few
  seconds; it caches afterward, and recomputes are fast.
- Equity/signal curves are **downsampled to ~800 points** for charting; all
  metrics are computed on the full daily series.
- Multi-leverage and multi-MA sweeps work here too — enter several MA numbers
  (e.g. `100 161 200`) and Ctrl/Cmd-click multiple leverages.
- Same research caveats as the CLI: synthetic leveraged series, pre-2010 figures
  are stress tests, leveraged ETFs are high-risk. Not investment advice.
