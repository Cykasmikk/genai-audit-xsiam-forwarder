# Changelog

All notable changes to this project will be documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
the project follows [Semantic Versioning](https://semver.org).

## [Unreleased]

## [0.1.0] — 2026-05-09

Initial public release.

### Added

- **Five ingestion feeds across two vendors plus an OpenTelemetry
  collector for Claude Code / Cowork inference visibility:**
  - `anthropic` — Anthropic Compliance API Activity Feed (Rev J 2026-04-20)
  - `anthropic_chats` — Anthropic Compliance API chat content (Rev J)
  - `openai` — OpenAI Audit Logs API
  - `openai_conversations` — OpenAI Compliance Logs Platform conversations
    (skeleton — endpoint not publicly documented; see
    [docs/coverage.md](docs/coverage.md#coverage-gaps))
  - `cowork-otel/` — standalone OpenTelemetry Collector deployment for
    Anthropic Cowork backend + Claude Code workstations

- **Vendor-adapter pattern** under `src/forwarder/vendors/` so adding a
  third vendor is one new file plus a dispatch entry.

- **Multi-cloud ingestion via Palo Alto Networks' documented native
  patterns:**
  - AWS: Lambda → S3 (gzipped JSON-lines) → S3 ObjectCreated → SQS →
    XSIAM pulls via cross-account IAM role with external ID
  - GCP: Cloud Function (Gen 2) → Pub/Sub → XSIAM pulls via dedicated
    pull subscription with a service-account credential file
  - Fallback: direct POST to XSIAM HTTP Collector via `egress/http.py`

- **Idempotency model** with timestamp watermark + content-hash dedupe
  using a 5-minute overlap window. State namespaced per vendor in
  DynamoDB / Firestore.

- **Per-vendor concurrency caps** (Lambda
  `reserved_concurrent_executions = 1`, Cloud Function
  `max_instance_count = 1`) so a slow tick never races the next.
  Cross-vendor parallelism preserved.

- **Documentation** under `docs/` covering architecture, coverage,
  per-cloud deployment guides, on-call operations runbook, security
  threat model, per-vendor setup detail, and XSIAM-side integration
  with XQL recipe library.

- **Quality gates in CI:**
  - 47 deterministic smoke tests (per-vendor, cross-vendor, parallel
    execution; runs without AWS/GCP credentials)
  - Coverage threshold ≥ 75 % on `coverage.py`
  - `ruff check` + `ruff format --check`
  - `bandit` Python SAST
  - `pip-audit` dependency CVE scanner
  - `terraform fmt -check` + `terraform validate` on all four stacks
  - `checkov` IaC security scanner on all four stacks
  - `gitleaks` for committed-secret detection
  - Pre-commit hooks mirroring the CI gates locally

- **Live-deployed and end-to-end verified** on AWS account
  `*****` and GCP project `***`. Both
  destroyed cleanly after testing; total live-test cost a few cents.

### Documented gaps

- **OpenAI Conversations adapter is a skeleton.** The Compliance Logs
  Platform conversations endpoint is gated behind OpenAI Enterprise and
  not publicly documented. The adapter ships with educated defaults
  aligned with the sibling Audit Logs API and is overridable via env
  var (`OPENAI_CONVERSATIONS_PATH`). Recommended path for production
  is Palo Alto Networks' native XSIAM "OpenAI ChatGPT Enterprise
  Compliance" integration if available.

- **Cowork OTel collector** has been built and validates clean but has
  not been live-deployed end-to-end. Recommended to do a live test
  before relying on it in production.

- **Programmatic API call bodies** (Claude API, OpenAI API) are not
  retained server-side by either vendor. To audit them, use
  application-side logging or a logging proxy upstream of the API.

[Unreleased]: https://github.com/Cykasmikk/genai-audit-xsiam-forwarder/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Cykasmikk/genai-audit-xsiam-forwarder/releases/tag/v0.1.0
