data "aws_caller_identity" "current" {}

# A random external ID strengthens the cross-account trust policy beyond
# "you know the role ARN." It's pasted into the XSIAM data source config
# alongside the role ARN and SQS URL.
resource "random_uuid" "xsiam_external_id" {}

# ─── State table (watermark + recent content hashes) ──────────────────────
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

# ─── Anthropic API key secret ─────────────────────────────────────────────
resource "aws_secretsmanager_secret" "anthropic_key" {
  name_prefix = "${var.name_prefix}/anthropic-admin-key-"
  description = "Anthropic Admin API key used to read Claude Compliance API events."
}

resource "aws_secretsmanager_secret_version" "anthropic_key" {
  secret_id     = aws_secretsmanager_secret.anthropic_key.id
  secret_string = var.anthropic_admin_api_key
}

# ─── Audit bucket (XSIAM pulls from this) ─────────────────────────────────
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

# ─── SQS queue for ObjectCreated notifications ────────────────────────────
resource "aws_sqs_queue" "audit_dlq" {
  name                       = "${var.name_prefix}-audit-dlq"
  message_retention_seconds  = 1209600 # 14 days
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 60
}

resource "aws_sqs_queue" "audit" {
  name                       = "${var.name_prefix}-audit"
  message_retention_seconds  = 345600 # 4 days
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 60

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.audit_dlq.arn
    maxReceiveCount     = 5
  })
}

# Allow the bucket to publish ObjectCreated events to the queue.
data "aws_iam_policy_document" "sqs_from_s3" {
  statement {
    sid     = "AllowS3Notify"
    actions = ["sqs:SendMessage"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    resources = [aws_sqs_queue.audit.arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.audit.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "audit" {
  queue_url = aws_sqs_queue.audit.id
  policy    = data.aws_iam_policy_document.sqs_from_s3.json
}

resource "aws_s3_bucket_notification" "audit" {
  bucket = aws_s3_bucket.audit.id

  queue {
    queue_arn = aws_sqs_queue.audit.arn
    events    = ["s3:ObjectCreated:*"]
  }

  depends_on = [aws_sqs_queue_policy.audit]
}

# ─── Cross-account role XSIAM assumes to pull from S3+SQS ─────────────────
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
  description        = "Assumed by Cortex XSIAM tenant to pull audit logs from S3 via SQS notifications."
  assume_role_policy = data.aws_iam_policy_document.xsiam_assume.json
}

data "aws_iam_policy_document" "xsiam" {
  statement {
    sid     = "ReadAuditObjects"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.audit.arn}/*",
    ]
  }
  statement {
    sid     = "ListBucket"
    actions = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [
      aws_s3_bucket.audit.arn,
    ]
  }
  statement {
    sid = "ConsumeNotifications"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]
    resources = [aws_sqs_queue.audit.arn]
  }
}

resource "aws_iam_role_policy" "xsiam" {
  role   = aws_iam_role.xsiam.id
  policy = data.aws_iam_policy_document.xsiam.json
}

# ─── Lambda IAM ───────────────────────────────────────────────────────────
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
  name               = "${var.name_prefix}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda" {
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
    resources = [aws_secretsmanager_secret.anthropic_key.arn]
  }

  statement {
    sid       = "WriteAuditObjects"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit.arn}/*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

# ─── Lambda package & function ────────────────────────────────────────────
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../../src"
  output_path = "${path.module}/.build/forwarder.zip"
  excludes = [
    "forwarder/__pycache__",
    "forwarder/egress/__pycache__",
    "forwarder/gcp_handler.py",
    "forwarder/state_gcp.py",
    "forwarder/egress/pubsub.py",
    "main.py",
    "requirements.txt",
  ]
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name_prefix}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "forwarder" {
  function_name    = var.name_prefix
  role             = aws_iam_role.lambda.arn
  handler          = "forwarder.aws_handler.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 300
  memory_size      = 512

  environment {
    variables = {
      STATE_TABLE              = aws_dynamodb_table.state.name
      ANTHROPIC_KEY_SECRET_ARN = aws_secretsmanager_secret.anthropic_key.arn
      AUDIT_BUCKET             = aws_s3_bucket.audit.bucket
      AUDIT_PREFIX             = "claude-compliance"
      INITIAL_LOOKBACK_MINUTES = tostring(var.initial_lookback_minutes)
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# ─── Schedule ─────────────────────────────────────────────────────────────
resource "aws_cloudwatch_event_rule" "tick" {
  name                = "${var.name_prefix}-tick"
  schedule_expression = "rate(${var.schedule_minutes} minutes)"
}

resource "aws_cloudwatch_event_target" "tick" {
  rule = aws_cloudwatch_event_rule.tick.name
  arn  = aws_lambda_function.forwarder.arn
}

resource "aws_lambda_permission" "tick" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.forwarder.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.tick.arn
}
