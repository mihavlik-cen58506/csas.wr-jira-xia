XIA Nightly Processor
=====================

Keboola custom component (`csas.wr-jira-xia`) that processes pending XIA archive requests
nightly: revalidates queued Jira issues via JQL, bulk-moves eligible issues into the Jira
`XIA` project, and persists the outcomes back to Keboola storage tables.

It replaces the previous Keboola Transformation logic ([context/XIA/xia_nightly_main.py](context/XIA/xia_nightly_main.py)),
which could no longer be used due to a company policy that disallows third-party API calls
from Transformations — such calls must run from a dedicated custom component.

The full functional specification and acceptance criteria live in [context/XIA/specs.md](context/XIA/specs.md).

What it does
============

For every request with `STATUS = PENDING` in the `XIA_REQUESTS` table (operation type
`ARCHIVE_TEST` — the only supported type in this version):

1. Revalidates the request's pending items against Jira in batches of 100 issues (JQL),
   skipping items that are already in `XIA`, in the wrong space, or the wrong issue type.
2. Bulk-moves the remaining eligible issues into the `XIA` project and polls the async
   Jira task until it completes or times out (max 3600s).
3. Updates item/request statuses (`MOVED`/`SKIPPED`/`FAILED`, `SUCCESS`/`PARTIAL_SUCCESS`/`FAILED`)
   and appends one row to `XIA_RUNS` recording the outcome.

A crash-recovery step also resets any request stuck in `RUNNING` (from a previous failed run)
back to `PENDING` before processing starts. A failure while processing one request never
aborts the rest of the job — that request is simply marked `FAILED` and processing continues.

Configuration
=============

Exactly 3 required parameters (see [component_config/configSchema.json](component_config/configSchema.json)):

| Parameter          | Description                                             |
|---------------------|------------------------------------------------------------|
| `JIRA_BASE_URL`     | Jira base URL, e.g. `https://your-domain.atlassian.net`. |
| `JIRA_USERNAME`     | Jira account username/email used for Basic Auth.        |
| `#jira_api_token`   | Jira API token (encrypted in Keboola UI).                |

Input / output mapping
=======================

The component expects exactly 3 mapped tables on input, and writes the same 3 tables back
on output (full replace). Physical table names are never hardcoded — each role is resolved
by a case-insensitive substring match on the mapped table name, so names can vary by
environment as long as they contain:

- `XIA_REQUESTS`
- `XIA_REQUEST_ITEMS`
- `XIA_RUNS`

Required columns per table are listed in [context/XIA/specs.md](context/XIA/specs.md) §5; any
extra columns already present in the tables are passed through unchanged.

Development
-----------

Requires Docker. `docker-compose.yml` mounts the whole repo at `/code` and points
`KBC_DATADIR` at [component_config/sample-config](component_config/sample-config) (relative to
`/code`, the container's working directory) — so `docker-compose run --rm dev` uses that sample
data directly, no manual copying needed. To use a different local data folder instead, change
the `KBC_DATADIR` value in `docker-compose.yml`.

Clone this repository, initialize the workspace, and run the component using the following
commands:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
git clone https://github.com/mihavlik-cen58506/csas.wr-jira-xia.git
cd csas.wr-jira-xia
docker-compose build
docker-compose run --rm dev
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The sample config in [component_config/sample-config](component_config/sample-config) has
placeholder Jira credentials and sample input tables. Note that `src/jira_api.py` has no
mock/dry-run mode, so running against it will make **real Jira API calls** — replace the
placeholder credentials and issue IDs with valid ones in your own Jira instance/sandbox
before running `docker-compose run --rm dev` end-to-end. Any output tables the component
writes land in `component_config/sample-config/out/` (git-ignored — never committed).

Run the test suite and perform lint checks using this command (mocked Jira client, no network calls):

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
docker-compose run --rm test
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Or locally without Docker:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
uv run pytest tests/ -q
uv run ruff check
uv run ruff format --check
uv run ty check
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

### Running with Podman instead of Docker

No changes to this repo are needed — [Dockerfile](Dockerfile) and `docker-compose.yml` use
only plain, portable syntax (standard multi-stage build, standard compose `services`/`volumes`/
`environment` keys), so Podman Desktop can build and run them as-is.

1. Install [Podman Desktop](https://podman-desktop.io/) and initialize its machine (Windows:
   done automatically on first launch; it provisions a WSL2-backed Podman machine).
2. Either enable **Docker compatibility** in Podman Desktop settings (ships a `docker` CLI shim)
   and keep using the exact `docker-compose ...` commands above, **or** use Podman's own compose
   command directly:
   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
   podman compose build
   podman compose run --rm dev
   podman compose run --rm test
   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
3. No extra parameters or files are required — the same `KBC_DATADIR`/volume setup in
   `docker-compose.yml` applies unchanged; only the CLI you invoke it with differs.

Note: CI ([.github/workflows/push.yml](.github/workflows/push.yml)) always builds/tests with
real Docker on GitHub-hosted runners, regardless of what you use locally — Podman vs Docker is
purely a local developer-machine choice.

Integration
===========

CI/CD builds, tests and deploys the component via a reusable Keboola workflow
([.github/workflows/push.yml](.github/workflows/push.yml)) — pushes to non-main branches run
build+test, and semantic version tags (e.g. `1.0.0`) additionally push the image and deploy it
to the Keboola Developer Portal under vendor `csas`.

For general details about deployment and integration with Keboola, refer to the
[deployment section of the developer
documentation](https://developers.keboola.com/extend/component/deployment/).
