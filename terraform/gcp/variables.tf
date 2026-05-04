variable "project_id" {
  description = "GCP project to deploy the forwarder into."
  type        = string
}

variable "region" {
  description = "GCP region for the Cloud Function & Scheduler."
  type        = string
  default     = "us-central1"
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

variable "schedule_minutes" {
  description = "Cloud Scheduler cadence in minutes."
  type        = number
  default     = 5
}

variable "initial_lookback_minutes" {
  description = "On first run (no state), how far back to pull events from."
  type        = number
  default     = 60
}

variable "subscription_message_retention_seconds" {
  description = <<-EOT
    Retention on the XSIAM-bound subscription. Long enough to absorb XSIAM
    outages (default: 7 days, the Pub/Sub maximum).
  EOT
  type        = number
  default     = 604800
}
