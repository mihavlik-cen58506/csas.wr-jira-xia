# AGENTS.md — XIA Nightly Processor (Keboola Component)

## What this component does
This is a **Keboola custom component** (`csas.wr-jira-xia`) that replaces the Python
Transformation logic in [context/XIA/xia_nightly_main.py](context/XIA/xia_nightly_main.py).
It processes pending XIA archive requests nightly: revalidates queued Jira issues via JQL,
bulk-moves eligible issues into the Jira `XIA` project, and persists outcomes back to 3
Keboola storage tables (`XIA_REQUESTS`, `XIA_REQUEST_ITEMS`, `XIA_RUNS`).

**The full spec is [context/XIA/specs.md](context/XIA/specs.md)** — the acceptance criteria
(AC1–AC6) there are the reference for expected behavior. The `src/` implementation is
**complete and spec-compliant** (V1 scope, `ARCHIVE_TEST` operation only) — this is no longer
cookiecutter boilerplate.

Reference implementation (old Transformation this component replaces): [context/XIA/xia_nightly_main.py](context/XIA/xia_nightly_main.py).

## Source layout
- `src/component.py` — entrypoint (`Component` class extending `ComponentBase`); resolves the
  3 tables, builds the `JiraApiClient`, calls `processor.process_pending_requests`, writes outputs.
- `src/configuration.py` — Pydantic `Configuration` model (`JIRA_BASE_URL`, `#jira_api_token`).
- `src/jira_api.py` — Jira REST v3 client (JQL search, bulk move, task polling, error parsing).
- `src/processor.py` — business orchestration: `process_pending_requests` (crash recovery + main
  loop), `process_request` / `_process_archive_test` (per-request flow), `_fail_request` (shared
  FAILED-branch helper), `derive_run_status` / `derive_request_status` (status rules).
- `src/csv_io.py` — table read/write helpers, resolves table roles from Input Mapping by name match.
- `src/constants.py` — status/skip-reason/timing constants.

## Key domain rules
- Exactly 2 config params: `JIRA_BASE_URL` (Jira API gateway URL, e.g. `https://api.atlassian.com/ex/jira/{cloud_id}`), `JIRA_API_TOKEN` (service account token, encrypted, alias `#jira_api_token`, sent as `Authorization: Bearer`).
- Input/output tables are resolved by **case-insensitive substring match** on mapped table name
  (`XIA_REQUESTS`, `XIA_REQUEST_ITEMS`, `XIA_RUNS`), never hardcoded — fail fast with `UserException` if a role is missing/ambiguous.
- Outputs are full-replace; unrecognized columns must pass through unchanged.
- Timestamps: Europe/Prague timezone, formatted `YYYY-MM-DD HH:MM:SS` (`processor.now_prague`).
- JQL revalidation batches of exactly 100 `ISSUE_ID`s; polling interval 5s, max wait 3600s.
- Per-request exceptions must not crash the whole job — mark that request `FAILED` and continue.
- Never log `JIRA_API_TOKEN` or the full `Authorization` header.

## Conventions
- Python `~=3.14.0`, dependency management via `uv` (see [pyproject.toml](pyproject.toml), `uv.lock`).
- Config validation uses **pydantic** `BaseModel` with field aliases for encrypted params (e.g. `#jira_api_token`) — see [src/configuration.py](src/configuration.py) for the existing pattern (raises `UserException` on `ValidationError`).
- Logging via stdlib `logging`, lazy `%s` formatting (ruff `G` rule enforced, no f-strings in log calls).
- Imports sorted per ruff `I` rule; modern syntax per `UP` rule. Line length 120.
- Docstrings are short "what/why" one-liners — no spec section references inline; consult [context/XIA/specs.md](context/XIA/specs.md) directly for authoritative detail.
- Tests live in `tests/`: `test_processor.py` covers business logic (AC1–AC6, mocked `JiraApiClient`); `test_component.py` covers the entrypoint with `mock.patch.dict(os.environ, {"KBC_DATADIR": ...})` + `freezegun`.

## Developer workflow
- Build/run dev shell: `docker-compose run --rm dev`
- Run tests + lint: `docker-compose run --rm test` (runs `pytest tests/`) — requires Docker Desktop running.
- Locally (outside Docker): `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run ty check` — these are also the pre-commit hooks ([.pre-commit-config.yaml](.pre-commit-config.yaml)).
- Sample local data folder: [component_config/sample-config](component_config/sample-config) (config.json with placeholder Jira credentials, `in/tables/*.csv` sample rows) — copy pattern used by `KBC_DATADIR`. Running `docker-compose run --rm dev` against it will make **real Jira API calls**; there is no mock/dry-run mode in `jira_api.py`.
- CI/CD is delegated to a reusable Keboola workflow: [.github/workflows/push.yml](.github/workflows/push.yml) (`keboola/component-ci@master`), with `app_id: csas.wr-jira-xia`, `vendor: csas`. Triggers on push to non-main branches (build+test) and on semver tags (build+test+deploy). Requires repo-level `KBC_DEVELOPERPORTAL_USERNAME` (variable) and `KBC_DEVELOPERPORTAL_PASSWORD` (secret) to deploy.

## When implementing
- Cross-check changes against [context/XIA/specs.md](context/XIA/specs.md) acceptance criteria (AC1–AC6) before considering a change complete.
- Keep Jira API error parsing consistent with the structured extraction already prototyped in `context/XIA/xia_nightly_main.py` (`errorMessages`, `errors`, fallback keys `message`/`errorMessage`/`error_description`/`error`).
- Update `component_config/configSchema.json` / `configRowSchema.json` if the `Configuration` model's fields change — these must stay in sync.

