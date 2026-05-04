locals {
  required_apis = [
    "cloudfunctions.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "firestore.googleapis.com",
    "eventarc.googleapis.com",
    "pubsub.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.required_apis)
  service            = each.value
  disable_on_destroy = false
}

# ─── Firestore (state store) ──────────────────────────────────────────────
resource "google_firestore_database" "state" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.apis]
}

# ─── Anthropic API key secret ─────────────────────────────────────────────
resource "google_secret_manager_secret" "anthropic_key" {
  secret_id = "${var.name_prefix}-anthropic-admin-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "anthropic_key" {
  secret      = google_secret_manager_secret.anthropic_key.id
  secret_data = var.anthropic_admin_api_key
}

# ─── Audit Pub/Sub topic + XSIAM-bound subscription ───────────────────────
resource "google_pubsub_topic" "audit" {
  name       = "${var.name_prefix}-audit"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_subscription" "xsiam" {
  name  = "${var.name_prefix}-xsiam"
  topic = google_pubsub_topic.audit.id

  message_retention_duration = "${var.subscription_message_retention_seconds}s"
  retain_acked_messages      = false
  ack_deadline_seconds       = 60
  enable_message_ordering    = false

  expiration_policy {
    ttl = "" # never expire
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}

# ─── Service account XSIAM uses to pull from the subscription ─────────────
resource "google_service_account" "xsiam" {
  account_id   = "${var.name_prefix}-xsiam"
  display_name = "Cortex XSIAM ingest"
  description  = "Authenticates the Cortex XSIAM tenant pulling Claude audit events from Pub/Sub."
}

resource "google_pubsub_subscription_iam_member" "xsiam_subscriber" {
  subscription = google_pubsub_subscription.xsiam.id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.xsiam.email}"
}

resource "google_pubsub_topic_iam_member" "xsiam_viewer" {
  topic  = google_pubsub_topic.audit.id
  role   = "roles/pubsub.viewer"
  member = "serviceAccount:${google_service_account.xsiam.email}"
}

# ─── Function service account ─────────────────────────────────────────────
resource "google_service_account" "fn" {
  account_id   = "${var.name_prefix}-fn"
  display_name = "Claude → Pub/Sub forwarder"
}

resource "google_secret_manager_secret_iam_member" "fn_anthropic" {
  secret_id = google_secret_manager_secret.anthropic_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.fn.email}"
}

resource "google_project_iam_member" "fn_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.fn.email}"
}

resource "google_pubsub_topic_iam_member" "fn_publisher" {
  topic  = google_pubsub_topic.audit.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.fn.email}"
}

# ─── Function source archive ──────────────────────────────────────────────
data "archive_file" "fn_src" {
  type        = "zip"
  source_dir  = "${path.module}/../../src"
  output_path = "${path.module}/.build/forwarder.zip"
  excludes = [
    "forwarder/__pycache__",
    "forwarder/egress/__pycache__",
    "forwarder/aws_handler.py",
    "forwarder/state_aws.py",
    "forwarder/egress/s3.py",
  ]
}

resource "google_storage_bucket" "src" {
  name                        = "${var.project_id}-${var.name_prefix}-src"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
}

resource "google_storage_bucket_object" "src" {
  name   = "forwarder-${data.archive_file.fn_src.output_md5}.zip"
  bucket = google_storage_bucket.src.name
  source = data.archive_file.fn_src.output_path
}

# ─── Cloud Function (Gen 2) ───────────────────────────────────────────────
resource "google_cloudfunctions2_function" "forwarder" {
  name     = var.name_prefix
  location = var.region

  build_config {
    runtime     = "python312"
    entry_point = "handler"
    source {
      storage_source {
        bucket = google_storage_bucket.src.name
        object = google_storage_bucket_object.src.name
      }
    }
  }

  service_config {
    available_memory      = "512M"
    timeout_seconds       = 540
    service_account_email = google_service_account.fn.email
    environment_variables = {
      GCP_PROJECT              = var.project_id
      ANTHROPIC_KEY_SECRET     = "${google_secret_manager_secret.anthropic_key.id}/versions/latest"
      AUDIT_TOPIC              = google_pubsub_topic.audit.name
      INITIAL_LOOKBACK_MINUTES = tostring(var.initial_lookback_minutes)
    }
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic   = google_pubsub_topic.tick.id
    retry_policy   = "RETRY_POLICY_DO_NOT_RETRY"
  }

  depends_on = [google_project_service.apis, google_firestore_database.state]
}

# ─── Schedule (Cloud Scheduler → Pub/Sub tick → Function) ─────────────────
resource "google_pubsub_topic" "tick" {
  name       = "${var.name_prefix}-tick"
  depends_on = [google_project_service.apis]
}

resource "google_cloud_scheduler_job" "tick" {
  name      = "${var.name_prefix}-tick"
  region    = var.region
  schedule  = "*/${var.schedule_minutes} * * * *"
  time_zone = "UTC"

  pubsub_target {
    topic_name = google_pubsub_topic.tick.id
    data       = base64encode("{}")
  }

  depends_on = [google_project_service.apis]
}
