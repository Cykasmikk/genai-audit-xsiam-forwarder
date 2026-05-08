locals {
  required_apis = [
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "pubsub.googleapis.com",
    "iam.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.required_apis)
  service            = each.value
  disable_on_destroy = false
}

# ─── Bearer token ─────────────────────────────────────────────────────────
resource "random_password" "bearer_token" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret" "bearer_token" {
  secret_id = "${var.name_prefix}-bearer-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "bearer_token" {
  secret      = google_secret_manager_secret.bearer_token.id
  secret_data = random_password.bearer_token.result
}

# ─── Pub/Sub topic + XSIAM-bound subscription ─────────────────────────────
resource "google_pubsub_topic" "cowork" {
  name       = var.name_prefix
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_subscription" "xsiam" {
  name                       = "${var.name_prefix}-xsiam"
  topic                      = google_pubsub_topic.cowork.id
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
  subscription = google_pubsub_subscription.xsiam.id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${var.xsiam_service_account_email}"
}

resource "google_pubsub_topic_iam_member" "xsiam_viewer" {
  topic  = google_pubsub_topic.cowork.id
  role   = "roles/pubsub.viewer"
  member = "serviceAccount:${var.xsiam_service_account_email}"
}

# ─── Service account for the Cloud Run collector ──────────────────────────
resource "google_service_account" "collector" {
  account_id   = "${var.name_prefix}-svc"
  display_name = "Cowork OTel collector"
}

resource "google_secret_manager_secret_iam_member" "collector_token" {
  secret_id = google_secret_manager_secret.bearer_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.collector.email}"
}

resource "google_pubsub_topic_iam_member" "collector_publisher" {
  topic  = google_pubsub_topic.cowork.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.collector.email}"
}

# ─── Collector config (rendered) stored in Secret Manager ─────────────────
locals {
  collector_config = templatefile("${path.module}/../../collector-config.yaml.tftpl", {
    cloud       = "gcp"
    aws_region  = ""
    aws_bucket  = ""
    aws_prefix  = ""
    gcp_project = var.project_id
    gcp_topic   = google_pubsub_topic.cowork.name
  })
}

resource "google_secret_manager_secret" "collector_config" {
  secret_id = "${var.name_prefix}-config"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "collector_config" {
  secret      = google_secret_manager_secret.collector_config.id
  secret_data = local.collector_config
}

resource "google_secret_manager_secret_iam_member" "collector_config_access" {
  secret_id = google_secret_manager_secret.collector_config.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.collector.email}"
}

# ─── Cloud Run service ────────────────────────────────────────────────────
resource "google_cloud_run_v2_service" "collector" {
  name     = var.name_prefix
  location = var.region

  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.collector.email

    scaling {
      min_instance_count = 1
      max_instance_count = 10
    }

    containers {
      image = var.collector_image
      args  = ["--config=env:COLLECTOR_CONFIG"]

      ports {
        container_port = 4318
      }

      env {
        name = "OTEL_AUTH_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.bearer_token.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "COLLECTOR_CONFIG"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.collector_config.secret_id
            version = "latest"
          }
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      startup_probe {
        http_get {
          path = "/"
          port = 13133
        }
        initial_delay_seconds = 5
        period_seconds        = 10
        timeout_seconds       = 5
        failure_threshold     = 6
      }
    }
  }

  depends_on = [google_project_service.apis]
}

# Allow public unauthenticated traffic — the collector enforces its own
# bearer-token auth on /v1/logs. Cowork backend & developer workstations
# need to reach it without GCP IAM credentials.
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.collector.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
