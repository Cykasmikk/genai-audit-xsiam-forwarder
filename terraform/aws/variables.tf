variable "region" {
  description = "AWS region for the forwarder."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix applied to all resources."
  type        = string
  default     = "genai-audit-xsiam-forwarder"
}

variable "vendors" {
  description = <<-EOT
    Map of vendor name → vendor-specific NON-SENSITIVE config. Each entry
    creates a dedicated Lambda, EventBridge schedule, secret, SQS audit
    queue, and S3 prefix. Only listed vendors are deployed.

    Supported vendor keys: "anthropic" (Compliance API Activity Feed),
    "anthropic_chats" (Compliance API chat content — needs sk-ant-api01-
    Compliance Access Key), "openai" (Audit Logs API), "openai_conversations"
    (Compliance Logs Platform conversation logs — partial spec, see
    src/forwarder/vendors/openai_conversations.py docstring).

    API keys are passed separately via var.api_keys (sensitive). Terraform
    forbids sensitive values as for_each keys, so they're split.
  EOT
  type = map(object({
    schedule_minutes         = optional(number, 5)
    initial_lookback_minutes = optional(number, 60)
  }))

  validation {
    condition     = alltrue([for k in keys(var.vendors) : contains(["anthropic", "anthropic_chats", "openai", "openai_conversations"], k)])
    error_message = "vendors map keys must be one of: anthropic, anthropic_chats, openai, openai_conversations."
  }
  validation {
    condition     = length(var.vendors) > 0
    error_message = "Provide at least one vendor in var.vendors."
  }
}

variable "api_keys" {
  description = <<-EOT
    Map of vendor name → API key. Keys must match those in var.vendors.

    Anthropic: sk-ant-admin01-... (Admin key) or sk-ant-api01-... (Compliance).
    OpenAI:    sk-admin-...
  EOT
  type        = map(string)
  sensitive   = true

  validation {
    condition     = alltrue([for k in keys(var.api_keys) : contains(["anthropic", "anthropic_chats", "openai", "openai_conversations"], k)])
    error_message = "api_keys map keys must be one of: anthropic, anthropic_chats, openai, openai_conversations."
  }
}

variable "xsiam_aws_account_id" {
  description = <<-EOT
    AWS account ID of the Cortex XSIAM tenant that will assume the cross-
    account IAM role. Shown in the XSIAM "Amazon S3 generic logs" data
    source onboarding screen.
  EOT
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch log retention for each Lambda. Default 365 to align with typical SOC-2 / ISO retention requirements."
  type        = number
  default     = 365
}

variable "bucket_object_retention_days" {
  description = <<-EOT
    Lifecycle rule applied to audit objects in the shared S3 bucket. Set 0
    to disable expiration. Source-side retention varies by vendor (Anthropic
    Compliance API: 6 years; OpenAI: per their data retention policy).
  EOT
  type        = number
  default     = 365
}
