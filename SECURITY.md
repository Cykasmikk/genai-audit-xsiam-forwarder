# Security policy

## Supported versions

Only the latest tagged release receives security fixes. Older tags are
provided as-is.

| Version | Supported |
|---|---|
| Latest tag (`v0.1.x`) | ✅ |
| Older | ❌ |

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Email the maintainer directly with the subject line
`SECURITY: genai-audit-xsiam-forwarder`. Include:

1. The affected component (which Terraform stack / Python module / doc).
2. A reproducer or proof-of-concept if you have one.
3. The impact you've assessed (data exposure, privilege escalation,
   denial of service, etc.).
4. Whether the issue is also present in upstream dependencies (boto3,
   google-cloud-pubsub, urllib3, OTel Collector, Anthropic / OpenAI APIs)
   so we can coordinate disclosure with them.

Acknowledgement target: 3 business days. Fix-or-mitigation timeline
target: 30 days for high severity, 90 days for medium, best-effort for
low.

## In-scope

- The Python forwarder (`src/forwarder/`)
- The Terraform stacks (`terraform/`, `cowork-otel/terraform/`)
- The OTel Collector configuration (`cowork-otel/collector-config.yaml.tftpl`)
- The CI workflow (`.github/workflows/ci.yml`)
- The pre-commit configuration (`.pre-commit-config.yaml`)

## Out of scope

- Vulnerabilities in upstream vendor APIs (Anthropic, OpenAI, AWS, GCP,
  Palo Alto Cortex XSIAM) — report directly to the vendor.
- Vulnerabilities in upstream Python or Terraform dependencies — report
  to the maintainers; we'll bump our pin once a fix is available.
- Misconfiguration in operator deploys (e.g. setting
  `force_destroy = true` in production, granting overly broad IAM)
  unless caused by a default in this repo.

## Threat model summary

See [docs/security.md](docs/security.md) for the documented threat model,
trust boundaries, IAM scopes, and data-classification matrix.

## Coordinated disclosure

We prefer 90-day coordinated disclosure. If you've already disclosed
elsewhere or to a vendor, let us know in the report so we can align.
