output "collector_endpoint" {
  description = "Public HTTPS endpoint Cowork / Claude Code agents connect to. Append /v1/logs for OTLP HTTP."
  value       = google_cloud_run_v2_service.collector.uri
}

output "bearer_token" {
  description = "Token Cowork / Claude Code agents present in Authorization header."
  value       = random_password.bearer_token.result
  sensitive   = true
}

output "cowork_topic" {
  description = "Pub/Sub topic the collector publishes to."
  value       = google_pubsub_topic.cowork.id
}

output "cowork_subscription" {
  description = "XSIAM-bound subscription. Configure an XSIAM 'GCP Pub/Sub' data source against this."
  value       = google_pubsub_subscription.xsiam.id
}
