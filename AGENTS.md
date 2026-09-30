# Agent instructions: petrosa-cio

Sovereign Gatekeeper and Strategy Controller for the Petrosa Fund.

Ecosystem rules (data pillars, commit and PR process, wording, memory) are in the umbrella [AGENTS.md](https://github.com/PetroSa2/petrosa/blob/main/AGENTS.md); this file covers this repository only. Only statements that can be checked against the repository are listed as facts.

## Commands (from the Makefile)

| Command | Purpose |
|---|---|
| `make setup` | Complete environment setup with dependencies |
| `make lint` | Run linting checks with ruff |
| `make format` | Format code with ruff |
| `make type-check` | Run type checking with mypy |
| `make test` | Run all tests with coverage |
| `make unit` | Run unit tests only |
| `make integration` | Run integration tests only |
| `make security` | Run security scans (Bandit) |
| `make pipeline` | Run complete CI/CD pipeline locally |
| `make test-quality` | Run test quality check (assertions check) |

Run the local pipeline or at least lint and tests before opening a pull request.

## Facts

- Python: `requires-python = ">=3.11"`; `.python-version` is `3.11`.
- Lint and format: ruff (config in `ruff.toml`).
- Type checking: mypy (config in `mypy.ini`).
- Tests: pytest, in `tests/`.
- `make test-quality` checks that tests contain assertions.
- Container image: built from `Dockerfile`.
- Instrumentation uses the internal `petrosa-otel` package.
- CI workflows: `.github/workflows/ci-checks.yml`, `.github/workflows/deploy.yml`, `.github/workflows/manual-deploy.yml`.

## Layout

Python packages at the top level: `cio/`. Also `tests/`, `docs/` and `scripts/` where they exist.

## Rules (policy)

- Do not add database drivers or connections to this service. Read and write data through the data-manager API.
- Commits use Conventional Commits; branches are `{type}/{issue-number}-{slug}`; a PR body contains `Closes #N`; never merge with `--admin`.
- Text that leaves the repository (PR titles and bodies, commit messages, code comments) uses generic roles such as Agentic Developer and never names the upstream workflow engine or its personas.
- Do not commit logs, drafts, scratch files or generated working notes. GitHub and the memory server are the record.
