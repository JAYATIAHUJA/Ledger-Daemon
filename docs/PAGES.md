# Public website and local app

The public website is https://jayatiahuja.github.io/Ledger-Daemon/.
GitHub Pages publishes the `docs` directory from `main`. No new deployment workflow is needed.

The website and local landing use `ledger_daemon/static/landing.html`, `landing.css`, and `landing.js` as their shared source. Edit those files, then export the public version. Do not maintain a second set of marketing copy in `docs/index.html`.

## Update the website files

From the repository root, start a fresh synthetic sample:

```sh
python -m ledger_daemon ui --out out/pages-sample --port 7043 --no-browser
```

In a second terminal, capture it:

```sh
python scripts/build_pages.py --url http://127.0.0.1:7043 --out docs
```

The exporter includes the landing page, a current sample workspace, local fonts and artwork, sample downloads, the proof manifest, and saved results from the original-proof and one-paise checks. Paths are relative so they work under `/Ledger-Daemon/`. Unrelated files in `docs` are preserved.

To reproduce a website proof check, extract `sample-batch.zip`, save the downloaded proof JSON in that extracted directory beside `proof-manifest.json`, then run:

```sh
python -m ledger_daemon verify-proof path/to/extracted/ledger-proof-ORDER.json --sources path/to/extracted
```

Updating these local files does not publish them. The public website changes after the updated files reach `main` and the existing GitHub Pages build succeeds.

## What works where

| Website sample | Full local app |
| --- | --- |
| Browse saved orders, filter and search | Process the supplied payment files |
| Explore captured app tabs | View current batch results |
| Read and download saved payment proofs | Run the Python proof checker |
| Replay a recorded failed tamper check | Check a new changed proof |
| Read the review workflow | Save decisions and their audit history |

The website labels the saved sample and points to the local setup commands when a user wants to save a review. It never sends a review request to a nonexistent GitHub Pages backend. The command-line animation is a recording, clearly labelled as such.

For a fresh clone, the correct directory is `cd Ledger-Daemon`—`pyproject.toml` is at the repository root. Your own complete batch needs orders, gateway payments, and bank records; importing only a bank file does not supply all three.
