locals {
  # GCP service account account_id is capped at 30 chars and disallows
  # underscores; Cloud Functions Gen 2 names disallow underscores too.
  # Map each vendor key to a hyphenated form for resources that need it.
  vendor_dashed = { for k, _ in var.vendors : k => replace(k, "_", "-") }

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

# Project number — needed for the Google-managed service-agent identities below.
data "google_project" "this" {
  project_id = var.project_id
}

# ─── Cloud Build / Cloud Functions Gen 2 build prerequisites ──────────────
# Workspace org policies often strip the default grants Cloud Build expects,
# producing "Build failed: missing permission on the build service account"
# at function-deploy time. Grant explicitly so a fresh project deploys clean.
resource "google_project_iam_member" "compute_sa_cloudbuild_builder" {
  project    = var.project_id
  role       = "roles/cloudbuild.builds.builder"
  member     = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"
  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "compute_sa_log_writer" {
  project    = var.project_id
  role       = "roles/logging.logWriter"
  member     = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"
  depends_on = [google_project_service.apis]
}

# ─── Shared: Firestore (state, namespaced by vendor in doc id) ────────────
resource "google_firestore_database" "state" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.apis]
}

# ─── Shared: source archive bucket ────────────────────────────────────────
data "archive_file" "fn_src" {
  type        = "zip"
  source_dir  = "${path.module}/../../src"
  output_path = "${path.module}/.build/forwarder.zip"
  excludes = [
    "forwarder/__pycache__",
    "forwarder/egress/__pycache__",
    "forwarder/vendors/__pycache__",
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

# ─── Shared: dedicated SA that XSIAM authenticates as ─────────────────────
# Fixed short name: GCP SA account_id ≤ 30 chars, no underscores. var.name_prefix
# is 27 chars at default which leaves no room — use a fixed short id instead.
resource "google_service_account" "xsiam" {
  account_id   = "xsiam-ingest"
  display_name = "Cortex XSIAM ingest"
  description  = "Authenticates the Cortex XSIAM tenant pulling GenAI vendor audit events."
}

# ─── Per-vendor resources ─────────────────────────────────────────────────

# API key secret per vendor
resource "google_secret_manager_secret" "api_key" {
  for_each  = var.vendors
  secret_id = "${var.name_prefix}-${each.key}-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "api_key" {
  for_each    = var.vendors
  secret      = google_secret_manager_secret.api_key[each.key].id
  secret_data = var.api_keys[each.key]
}

# Per-vendor function service account.
# Short SA id: `fn-{vendor-with-hyphens}`. Max len: 3 + 20 (openai-conversations) = 23.
resource "google_service_account" "fn" {
  for_each     = var.vendors
  account_id   = "fn-${local.vendor_dashed[each.key]}"
  display_name = "${each.key} → Pub/Sub forwarder"
}

resource "google_secret_manager_secret_iam_member" "fn_api_key" {
  for_each  = var.vendors
  secret_id = google_secret_manager_secret.api_key[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.fn[each.key].email}"
}

resource "google_project_iam_member" "fn_firestore" {
  for_each = var.vendors
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = "serviceAccount:${google_service_account.fn[each.key].email}"
}

# Audit Pub/Sub topic per vendor (one XSIAM data source per topic)
resource "google_pubsub_topic" "audit" {
  for_each   = var.vendors
  name       = "${var.name_prefix}-${each.key}-audit"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic_iam_member" "fn_publisher" {
  for_each = var.vendors
  topic    = google_pubsub_topic.audit[each.key].id
  role     = "roles/pubsub.publisher"
  member   = "serviceAccount:${google_service_account.fn[each.key].email}"
}

resource "google_pubsub_subscription" "xsiam" {
  for_each = var.vendors
  name     = "${var.name_prefix}-${each.key}-xsiam"
  topic    = google_pubsub_topic.audit[each.key].id

  message_retention_duration = "${var.subscription_message_retention_seconds}s"
  retain_acked_messages      = false
  ack_deadline_seconds       = 60
  enable_message_ordering    = false

  expiration_policy {
    ttl = ""
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}

resource "google_pubsub_subscription_iam_member" "xsiam_subscriber" {
  for_each     = var.vendors
  subscription = google_pubsub_subscription.xsiam[each.key].id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.xsiam.email}"
}

resource "google_pubsub_topic_iam_member" "xsiam_viewer" {
  for_each = var.vendors
  topic    = google_pubsub_topic.audit[each.key].id
  role     = "roles/pubsub.viewer"
  member   = "serviceAccount:${google_service_account.xsiam.email}"
}

# Per-vendor scheduler tick topic (separate from the audit topic)
resource "google_pubsub_topic" "tick" {
  for_each   = var.vendors
  name       = "${var.name_prefix}-${each.key}-tick"
  depends_on = [google_project_service.apis]
}

resource "google_cloud_scheduler_job" "tick" {
  for_each  = var.vendors
  name      = "${var.name_prefix}-${each.key}-tick"
  region    = var.region
  schedule  = "*/${each.value.schedule_minutes} * * * *"
  time_zone = "UTC"

  pubsub_target {
    topic_name = google_pubsub_topic.tick[each.key].id
    data       = base64encode("{}")
  }

  depends_on = [google_project_service.apis]
}

# Per-vendor Cloud Function. Cloud Functions Gen 2 names disallow underscores
# (they back onto Cloud Run service names with the same constraint).
resource "google_cloudfunctions2_function" "forwarder" {
  for_each = var.vendors
  name     = "${var.name_prefix}-${local.vendor_dashed[each.key]}"
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
    service_account_email = google_service_account.fn[each.key].email

    # Cap to 1 active instance per vendor so a slow tick cannot race with the
    # next scheduled tick on the shared Firestore doc. Different vendors get
    # independent caps, so they continue to run truly in parallel — only
    # same-vendor overlap is serialized (Cloud Scheduler retries the Pub/Sub
    # delivery so no tick is lost when an instance is busy).
    max_instance_count               = 1
    max_instance_request_concurrency = 1

    environment_variables = {
      VENDOR                   = each.key
      GCP_PROJECT              = var.project_id
      API_KEY_SECRET           = "${google_secret_manager_secret.api_key[each.key].id}/versions/latest"
      AUDIT_TOPIC              = google_pubsub_topic.audit[each.key].name
      INITIAL_LOOKBACK_MINUTES = tostring(each.value.initial_lookback_minutes)
    }
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic   = google_pubsub_topic.tick[each.key].id
    # Use the per-vendor function SA as the trigger principal so we can grant
    # run.invoker to a known SA instead of the default compute SA.
    service_account_email = google_service_account.fn[each.key].email
    retry_policy          = "RETRY_POLICY_DO_NOT_RETRY"
  }

  depends_on = [
    google_project_service.apis,
    google_firestore_database.state,
    google_project_iam_member.compute_sa_cloudbuild_builder,
    google_project_iam_member.compute_sa_log_writer,
  ]
}

# Eventarc-triggered Cloud Functions (Gen 2) need the trigger principal to be
# able to invoke the underlying Cloud Run service.
resource "google_cloud_run_v2_service_iam_member" "fn_invoker" {
  for_each = var.vendors
  project  = var.project_id
  location = var.region
  name     = google_cloudfunctions2_function.forwarder[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.fn[each.key].email}"
}

# Pub/Sub push subscriptions backed by an OIDC token need the Pub/Sub service
# agent to act as a token-creator on the SA whose identity is being used.
resource "google_service_account_iam_member" "pubsub_token_creator" {
  for_each           = var.vendors
  service_account_id = google_service_account.fn[each.key].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
