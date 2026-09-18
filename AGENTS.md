# Agent Notes

## Running tests

- Prefer Makefile targets over ad-hoc `pytest` invocations.
- Load project env first (`direnv allow`, or `source .envrc` in a direnv shell). Plain `source .envrc` may warn about `source_up_if_exists`; that is fine if `DATABASE_URL` and `REDIS_URL` are set.
- Integration / Django DB + Playwright UI: `make test-integration`
- Unit: `make test-unit`
- Do not invent custom database drop/terminate sequences or one-off pytest flags unless the Makefile path is broken.
- The user may have `make dev` running, which holds the test database. If pytest fails because `test_test_analysis_bot` already exists or is being accessed by other users, wait for `make dev` to stop using it and retry. Do not kill sessions or drop the database yourself.

## UI snapshots

- Snapshots live under `tests/snapshots/` (e.g. `releases/...`).
- `take_snapshot` is for visual review in git, not a CI gate: mismatches rewrite the baseline and never fail the test.
- `SNAPSHOT_DIFF_RATIO` only decides when a rewrite is worth writing; below that, leave the file alone.
- The same snapshot path runs locally and on CI. CI rewrites stay in the job workspace and are not committed unless someone pulls them into a PR.
