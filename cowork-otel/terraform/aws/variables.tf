variable "region" {
  description = "AWS region for the Cowork OTel collector."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix applied to all resources."
  type        = string
  default     = "genai-audit-cowork-otel"
}

variable "audit_bucket" {
  description = <<-EOT
    Name of the audit S3 bucket from the parent terraform/aws stack. The
    collector writes to $${cowork_prefix} within this bucket so XSIAM can
    ingest it as a separate data source.
  EOT
  type        = string
}

variable "cowork_prefix" {
  description = "S3 key prefix for Cowork events (will live under {audit_bucket}/{cowork_prefix}/)."
  type        = string
  default     = "cowork"
}

variable "xsiam_aws_account_id" {
  description = "AWS account ID of the Cortex XSIAM tenant (same value as parent stack)."
  type        = string
}

variable "xsiam_external_id" {
  description = <<-EOT
    The external ID from the parent terraform/aws stack, so XSIAM can
    assume the same role to read Cowork objects too.
  EOT
  type        = string
  sensitive   = true
}

variable "vpc_id" {
  description = "VPC id where the ALB and ECS Fargate tasks run."
  type        = string
}

variable "public_subnet_ids" {
  description = "List of ≥2 public subnet ids in different AZs for the ALB."
  type        = list(string)
  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "ALB requires at least 2 subnets in different AZs."
  }
}

variable "acm_certificate_arn" {
  description = "ACM certificate ARN matching var.hostname for ALB HTTPS."
  type        = string
}

variable "hostname" {
  description = "Public DNS name pointed at the ALB (e.g. cowork-otel.soc.example.com). You manage the DNS record yourself outside this stack."
  type        = string
}

variable "task_count" {
  description = "Fargate desired task count (HA across AZs)."
  type        = number
  default     = 2
}

variable "collector_image" {
  description = "OTel Collector contrib image tag."
  type        = string
  default     = "otel/opentelemetry-collector-contrib:0.110.0"
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the collector. Default 365 to align with typical SOC-2 / ISO retention requirements."
  type        = number
  default     = 365
}
