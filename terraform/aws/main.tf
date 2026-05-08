data "aws_caller_identity" "current" {}

resource "random_uuid" "xsiam_external_id" {}

# ─── Shared: state table ──────────────────────────────────────────────────
resource "aws_dynamodb_table" "state" {
  name         = "${var.name_prefix}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# ─── Shared: audit bucket (vendor objects partitioned by prefix) ──────────
resource "aws_s3_bucket" "audit" {
  bucket_prefix = "${var.name_prefix}-"
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  count  = var.bucket_object_retention_days > 0 ? 1 : 0
  bucket = aws_s3_bucket.audit.id

  rule {
    id     = "expire-audit-objects"
    status = "Enabled"
    filter {}
    expiration {
      days = var.bucket_object_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# ─── Per-vendor: SQS queue (one XSIAM data source per vendor) ─────────────
resource "aws_sqs_queue" "audit_dlq" {
  for_each                   = var.vendors
  name                       = "${var.name_prefix}-${each.key}-dlq"
  message_retention_seconds  = 1209600
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 60
}

resource "aws_sqs_queue" "audit" {
  for_each                   = var.vendors
  name                       = "${var.name_prefix}-${each.key}"
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 60

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.audit_dlq[each.key].arn
    maxReceiveCount     = 5
  })
}

# Each per-vendor SQS queue accepts ObjectCreated notifications scoped to
# that vendor's prefix in the shared bucket.
data "aws_iam_policy_document" "sqs_from_s3" {
  for_each = var.vendors
  statement {
    sid     = "AllowS3Notify"
    actions = ["sqs:SendMessage"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    resources = [aws_sqs_queue.audit[each.key].arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.audit.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "audit" {
  for_each  = var.vendors
  queue_url = aws_sqs_queue.audit[each.key].id
  policy    = data.aws_iam_policy_document.sqs_from_s3[each.key].json
}

resource "aws_s3_bucket_notification" "audit" {
  bucket = aws_s3_bucket.audit.id

  dynamic "queue" {
    for_each = var.vendors
    content {
      queue_arn     = aws_sqs_queue.audit[queue.key].arn
      events        = ["s3:ObjectCreated:*"]
      filter_prefix = "${queue.key}/"
    }
  }

  depends_on = [aws_sqs_queue_policy.audit]
}

# ─── Shared: cross-account IAM role XSIAM assumes ────────────────────────
data "aws_iam_policy_document" "xsiam_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.xsiam_aws_account_id}:root"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [random_uuid.xsiam_external_id.result]
    }
  }
}

resource "aws_iam_role" "xsiam" {
  name               = "${var.name_prefix}-xsiam-ingest"
  description        = "Assumed by Cortex XSIAM to pull GenAI vendor audit logs."
  assume_role_policy = data.aws_iam_policy_document.xsiam_assume.json
}

data "aws_iam_policy_document" "xsiam" {
  # Object-level access scoped to vendor prefixes (one Resource per vendor).
  statement {
    sid     = "ReadAuditObjects"
    actions = ["s3:GetObject"]
    resources = [
      for v in keys(var.vendors) : "${aws_s3_bucket.audit.arn}/${v}/*"
    ]
  }
  statement {
    sid       = "ListBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.audit.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [for v in keys(var.vendors) : "${v}/*"]
    }
  }
  statement {
    sid = "ConsumeNotifications"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]
    resources = [for v in keys(var.vendors) : aws_sqs_queue.audit[v].arn]
  }
}

resource "aws_iam_role_policy" "xsiam" {
  role   = aws_iam_role.xsiam.id
  policy = data.aws_iam_policy_document.xsiam.json
}

# ─── Per-vendor: API key secret ───────────────────────────────────────────
resource "aws_secretsmanager_secret" "api_key" {
  for_each    = var.vendors
  name_prefix = "${var.name_prefix}/${each.key}-api-key-"
  description = "API key for the ${each.key} audit log forwarder."
}

resource "aws_secretsmanager_secret_version" "api_key" {
  for_each      = var.vendors
  secret_id     = aws_secretsmanager_secret.api_key[each.key].id
  secret_string = var.api_keys[each.key]
}

# ─── Per-vendor: Lambda IAM role + policy ─────────────────────────────────
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each           = var.vendors
  name               = "${var.name_prefix}-${each.key}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda" {
  for_each = var.vendors

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
  }
  statement {
    sid       = "State"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.state.arn]
  }
  statement {
    sid       = "Secret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.api_key[each.key].arn]
  }
  statement {
    sid       = "WriteAuditObjects"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit.arn}/${each.key}/*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  for_each = var.vendors
  role     = aws_iam_role.lambda[each.key].id
  policy   = data.aws_iam_policy_document.lambda[each.key].json
}

# ─── Per-vendor: Lambda + log group + EventBridge schedule ────────────────
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../../src"
  output_path = "${path.module}/.build/forwarder.zip"
  excludes = [
    "forwarder/__pycache__",
    "forwarder/egress/__pycache__",
    "forwarder/vendors/__pycache__",
    "forwarder/gcp_handler.py",
    "forwarder/state_gcp.py",
    "forwarder/egress/pubsub.py",
    "main.py",
    "requirements.txt",
  ]
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = var.vendors
  name              = "/aws/lambda/${var.name_prefix}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "forwarder" {
  for_each = var.vendors

  function_name    = "${var.name_prefix}-${each.key}"
  role             = aws_iam_role.lambda[each.key].arn
  handler          = "forwarder.aws_handler.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 300
  memory_size      = 512

  # Cap to 1 concurrent invocation per vendor so a slow tick (e.g. a paginated
  # backfill across thousands of events) cannot race with the next scheduled
  # tick on the shared state row. Different vendors get independent caps, so
  # they continue to run truly in parallel — only same-vendor overlap is
  # serialized. EventBridge retries throttled invocations for up to 24 h, so
  # no audit data is dropped on overlap; the next tick simply runs after the
  # in-flight one finishes.
  reserved_concurrent_executions = 1

  environment {
    variables = {
      VENDOR                   = each.key
      STATE_TABLE              = aws_dynamodb_table.state.name
      API_KEY_SECRET_ARN       = aws_secretsmanager_secret.api_key[each.key].arn
      AUDIT_BUCKET             = aws_s3_bucket.audit.bucket
      AUDIT_PREFIX             = "audit"
      INITIAL_LOOKBACK_MINUTES = tostring(each.value.initial_lookback_minutes)
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_cloudwatch_event_rule" "tick" {
  for_each            = var.vendors
  name                = "${var.name_prefix}-${each.key}-tick"
  schedule_expression = "rate(${each.value.schedule_minutes} minutes)"
}

resource "aws_cloudwatch_event_target" "tick" {
  for_each = var.vendors
  rule     = aws_cloudwatch_event_rule.tick[each.key].name
  arn      = aws_lambda_function.forwarder[each.key].arn
}

resource "aws_lambda_permission" "tick" {
  for_each      = var.vendors
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.forwarder[each.key].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.tick[each.key].arn
}
