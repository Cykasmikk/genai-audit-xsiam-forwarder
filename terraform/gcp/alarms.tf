# Optional Cloud Monitoring alert policies for the polling forwarder.
#
# Set var.notification_channel_id to wire up alert delivery. When unset,
# policies are still created but have no notification channels — useful
# for OK-state review during a drill.

variable "notification_channel_id" {
  description = <<-EOT
    Cloud Monitoring notification channel id (e.g. an existing Slack /
    email / PagerDuty channel). Leave empty to create policies without
    a channel — they'll fire and resolve in the console only.
  EOT
  type        = string
  default     = ""
}

variable "stale_invocation_threshold_minutes" {
  description = <<-EOT
    Alert if a feed has had zero Cloud Function invocations for this
    many minutes. Default 30 = 6 ticks at the default 5-min schedule.
  EOT
  type        = number
  default     = 30
}

# ─── Per-feed: function errors → alert ─────────────────────────────────────
resource "google_monitoring_alert_policy" "function_errors" {
  for_each     = var.vendors
  display_name = "${var.name_prefix}-${each.key}-errors"
  combiner     = "OR"

  conditions {
    display_name = "5xx error rate > 0 in 5 min"
    condition_threshold {
      filter          = <<-EOT
        resource.type = "cloud_run_revision" AND
        resource.labels.service_name = "${google_cloudfunctions2_function.forwarder[each.key].name}" AND
        metric.type = "run.googleapis.com/request_count" AND
        metric.labels.response_code_class = "5xx"
      EOT
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = var.notification_channel_id == "" ? [] : [var.notification_channel_id]
  enabled               = true

  documentation {
    content   = "Cloud Function ${each.key} returned 5xx. Check Cloud Logging for the actionable error message."
    mime_type = "text/markdown"
  }
}

# ─── Per-feed: stale heartbeat (no invocations) → alert ───────────────────
resource "google_monitoring_alert_policy" "function_stale" {
  for_each     = var.vendors
  display_name = "${var.name_prefix}-${each.key}-stale"
  combiner     = "OR"

  conditions {
    display_name = "No invocations in ${var.stale_invocation_threshold_minutes} min"
    condition_absent {
      filter   = <<-EOT
        resource.type = "cloud_run_revision" AND
        resource.labels.service_name = "${google_cloudfunctions2_function.forwarder[each.key].name}" AND
        metric.type = "run.googleapis.com/request_count"
      EOT
      duration = "${var.stale_invocation_threshold_minutes * 60}s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = var.notification_channel_id == "" ? [] : [var.notification_channel_id]
  enabled               = true

  documentation {
    content   = "Cloud Function ${each.key} has not run in ${var.stale_invocation_threshold_minutes} min. Verify Cloud Scheduler is enabled and the function is healthy."
    mime_type = "text/markdown"
  }
}
