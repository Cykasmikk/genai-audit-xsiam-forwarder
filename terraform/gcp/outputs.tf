output "function_name" {
  value = google_cloudfunctions2_function.forwarder.name
}

output "function_uri" {
  value = google_cloudfunctions2_function.forwarder.service_config[0].uri
}

output "scheduler_job" {
  value = google_cloud_scheduler_job.tick.name
}

# ─── Values to paste into the XSIAM "GCP Pub/Sub" data source ─────────────
output "xsiam_audit_topic" {
  description = "Pub/Sub topic where audit events are published."
  value       = google_pubsub_topic.audit.id
}

output "xsiam_audit_subscription" {
  description = "Pull subscription XSIAM consumes. Paste this into the data source 'subscription' field."
  value       = google_pubsub_subscription.xsiam.id
}

output "xsiam_service_account_email" {
  description = <<-EOT
    Service account XSIAM authenticates as. Generate a JSON key for this SA
    out-of-band (gcloud iam service-accounts keys create) and paste the JSON
    into the XSIAM data source 'credentials' field. Do NOT add a
    google_service_account_key resource here — that puts the key in TF state.
  EOT
  value       = google_service_account.xsiam.email
}
