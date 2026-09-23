# Validation and release gates

Run `uv sync --locked --group dev`, `uv run pytest`, `uv run research-intel --help`, `uv run research-intel doctor`, `uv build`, and `python scripts/public_scan.py` before release. The source and secret scans inspect publishable files; generated data and secrets must remain ignored. Check the fresh Git history separately before publishing.

CI uses synthetic tests and no account, model, media, or paid provider. A separate clean-room check must clone the local public export, install it, run the doctor, and complete a public-safe research task with evidence and a dossier. Optional live source tests are not CI acceptance shortcuts. Release only if packaging, clean-room, license, privacy, security, tests, and documentation pass.
