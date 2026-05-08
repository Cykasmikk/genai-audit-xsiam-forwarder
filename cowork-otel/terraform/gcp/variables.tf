variable "project_id" {
  description = "GCP project to deploy into."
  type        = string
}

variable "region" {
  description = "Cloud Run region for the collector."
  type        = string
  default     = "us-central1"
}

variable "name_prefix" {
  description = "Prefix applied to all resources."
  type        = string
  default     = "genai-audit-cowork-otel"
}

variable "xsiam_service_account_email" {
  description = <<-EOT
    Email of the dedicated XSIAM SA from the parent terraform/gcp stack.
    Reused so XSIAM has subscriber access to the Cowork topic too.
  EOT
  type        = string
}

variable "collector_image" {
  description = "OTel Collector contrib image tag."
  type        = string
  default     = "otel/opentelemetry-collector-contrib:0.110.0"
}

variable "subscription_message_retention_seconds" {
  description = "Pub/Sub subscription retention (default 7 days, the Pub/Sub max)."
  type        = number
  default     = 604800
}
