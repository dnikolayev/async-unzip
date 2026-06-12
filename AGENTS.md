# Repository Guidelines

## Project Structure & Module Organization
Core logic lives in `async_unzip/unzipper.py`, exposed through the package init, while packaging metadata stays in `setup.py`. Keep reusable helpers in the `async_unzip` package—avoid scattering modules outside it unless absolutely necessary. Assets for manual testing sit under `tests/test_files` (sample ZIPs of different sizes and edge cases). Use `dist/` only for build artifacts; virtual environments such as `venv` or `aiofiles/` should remain untracked in Git to keep the tree clean.

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate`: create and enter an isolated environment before installing dependencies.
- `pip install -e .[dev]` (or `pip install -e . && pip install pytest pytest-cov`): install the package locally with test tooling so edits are immediately importable.
- `pytest -q`: run the test suite; add `-k pattern` when iterating on a specific scenario.
- `python -m build` (requires `pip install build`): produce sdist and wheel into `dist/` for release validation.

## Coding Style & Naming Conventions
Use 4-space indentation, module-level imports, and `snake_case` for functions/variables (`unzip`, `buffer_size`). Favor explicit `pathlib.Path` operations for file paths, and guard optional parameters with truthy checks as in existing code. Keep async I/O abstracted behind `async_open` so the `aiofile`/`aiofiles` swap stays centralized. When adding functionality, extend docstrings and keep logging/messages concise and user-facing.

## Testing Guidelines
Tests rely on `pytest`; name files `test_<feature>.py` inside `tests/`. Mirror real-world scenarios by referencing archives already stored in `tests/test_files` or add similarly small fixtures. Target functional coverage (e.g., large archives, directory creation, buffer overrides) and ensure async helpers are awaited. Before opening a PR, run `pytest --maxfail=1 --cov=async_unzip` to confirm coverage does not regress.

## Commit & Pull Request Guidelines
Follow the existing log style: short, imperative summaries (e.g., “Add aiofiles fallback”). Each PR should include a problem statement, a brief solution outline, testing evidence (command + result), and reference any related GitHub issues or tickets. Attach screenshots or logs only when the change alters user-visible behavior. Keep branches rebased on `master` to avoid noisy merge commits.

## Async I/O Dependencies & Configuration
The library expects either `aiofile` (preferred on Linux with `libaio1` installed) or `aiofiles`. Document which backend you exercised when testing, and note any OS-specific prerequisites in PR descriptions. When debugging missing dependencies, replicate the runtime check from `setup.py`/`unzipper.py` to provide actionable feedback to reviewers.
