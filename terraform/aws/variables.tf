variable "region" {
  description = "AWS region for the forwarder."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix applied to all resources."
  type        = string
  default     = "claude-xsiam-forwarder"
}

variable "anthropic_admin_api_key" {
  description = "Anthropic Admin API key (sk-ant-admin01-...) with Compliance API scope."
  type        = string
  sensitive   = true
}

variable "xsiam_aws_account_id" {
  description = <<-EOT
    AWS account ID of the Cortex XSIAM tenant that will assume the cross-account
    IAM role to pull from this bucket. The account ID is shown in the XSIAM
    "Amazon S3 generic logs" data source onboarding screen alongside the
    external ID field.
  EOT
  type        = string
}

variable "schedule_minutes" {
  description = "EventBridge schedule cadence in minutes."
  type        = number
  default     = 5
}

variable "initial_lookback_minutes" {
  description = "On first run (no state), how far back to pull events from."
  type        = number
  default     = 60
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the Lambda."
  type        = number
  default     = 90
}

variable "bucket_object_retention_days" {
  description = <<-EOT
    Lifecycle rule applied to audit objects in the S3 bucket. Set to 0 to
    disable expiration. SOC retention requirements drive this — the
    Compliance API itself retains 180 days of events server-side.
  EOT
  type        = number
  default     = 365
}
