# Contributing

Thanks for considering a contribution. This is internal SOC tooling
released as a reference implementation; we do accept PRs but the bar is
"production-grade or it doesn't merge."

## Quick local setup

```bash
git clone https://github.com/Cykasmikk/genai-audit-xsiam-forwarder.git
cd genai-audit-xsiam-forwarder

python3 -m venv .venv
.venv/bin/pip install -r src/requirements.txt boto3 ruff bandit pip-audit coverage pre-commit

# Pre-commit catches trailing whitespace, large files, secrets, lint, format.
.venv/bin/pre-commit install

# Smoke suite (no AWS/GCP creds needed):
PYTHONPATH=src .venv/bin/python tests/smoke.py
```

For Terraform local validate:

```bash
for d in terraform/aws terraform/gcp cowork-otel/terraform/aws cowork-otel/terraform/gcp; do
  terraform -chdir=$d init -backend=false -input=false
  terraform -chdir=$d fmt -check
  terraform -chdir=$d validate
done
```

## Quality gates that must stay green

CI enforces, and your PR must pass:

| Gate | What it catches |
|---|---|
| `ruff check` | Lint errors |
| `ruff format --check` | Formatter drift |
| `bandit -r src` | Python SAST findings |
| `pip-audit` | CVEs in pinned dependencies |
| `coverage --fail-under=75` | Coverage drop below 75 % on the smoke suite |
| `terraform fmt -check` (×4 stacks) | Terraform formatter drift |
| `terraform validate` (×4 stacks) | Terraform config errors |
| `checkov` (×4 stacks) | IaC security misconfig — annotate with `# checkov:skip=CKV_*:reason` if intentional |
| `gitleaks` | Accidentally-committed secrets |

If a check breaks because of an upstream tooling update (rule pack,
ruleset bump, etc.), fix it in the same PR or land it in a separate
PR labelled `ci-only` first.

## Adding a new vendor adapter

1. Drop a new file in `src/forwarder/vendors/`. Subclass nothing —
   implement the duck-typed `AuditClient` protocol from
   `vendors/__init__.py`:
   ```python
   class MyVendorClient:
       vendor = "my_vendor"
       def fetch_window(self, starting_at, ending_at): ...
   ```
2. Map the vendor's native event timestamp / id to the common
   `AuditEvent` (`id`, ISO `created_at`, `vendor`, `raw`).
3. Add it to the dispatch maps in `aws_handler.py` and `gcp_handler.py`.
4. Add the vendor key to the validation regex in
   `terraform/aws/variables.tf` and `terraform/gcp/variables.tf`
   (both `vendors` and `api_keys` validations).
5. Add Pub/Sub attribute extraction logic in `egress/pubsub.py`
   (`_<vendor>_attrs`) and `egress/http.py` (`_enrich`) if the schema
   has fields worth exposing as routing keys.
6. Add a test block in `tests/smoke.py` covering: key prefix
   validation positive + negative, URL/header construction, pagination,
   400/401/403/404 actionable errors, event normalization.
7. Add a section in `docs/coverage.md` documenting the event-type
   inventory and any gaps.
8. Add a `docs/vendors/<vendor>.md` if there's vendor-specific setup
   detail (enablement, key types, spec conformance).

## Issue labels

- `bug` — something is broken
- `enhancement` — new feature or capability
- `security` — see [SECURITY.md](SECURITY.md), do NOT use this label
  publicly for high-severity issues
- `docs` — documentation only
- `ci-only` — touches `.github/`, `.pre-commit-config.yaml`, or test
  tooling without changing behavior

## Commit message format

We prefer descriptive multi-paragraph commits over one-liners. For
non-trivial changes, the body should explain *why*, not *what* (the
diff explains what). Co-authored-by trailers are welcome.

## License

All contributions are accepted under the [Apache 2.0 license](LICENSE).
