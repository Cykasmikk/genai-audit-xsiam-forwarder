data "aws_caller_identity" "current" {}

# ─── Bearer token (Anthropic admin portal pastes this in) ─────────────────
resource "random_password" "bearer_token" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "bearer_token" {
  name_prefix = "${var.name_prefix}/bearer-token-"
  description = "Bearer token Cowork / Claude Code agents present to the collector."
}

resource "aws_secretsmanager_secret_version" "bearer_token" {
  secret_id     = aws_secretsmanager_secret.bearer_token.id
  secret_string = random_password.bearer_token.result
}

# ─── Collector config (rendered from the shared template) ─────────────────
locals {
  collector_config = templatefile("${path.module}/../../collector-config.yaml.tftpl", {
    cloud       = "aws"
    aws_region  = var.region
    aws_bucket  = var.audit_bucket
    aws_prefix  = "${var.cowork_prefix}/"
    gcp_project = ""
    gcp_topic   = ""
  })
}

resource "aws_secretsmanager_secret" "collector_config" {
  name_prefix = "${var.name_prefix}/collector-config-"
  description = "Rendered OTel Collector YAML config."
}

resource "aws_secretsmanager_secret_version" "collector_config" {
  secret_id     = aws_secretsmanager_secret.collector_config.id
  secret_string = local.collector_config
}

# ─── SQS queue for ObjectCreated notifications under cowork/ prefix ───────
resource "aws_sqs_queue" "cowork_dlq" {
  name                       = "${var.name_prefix}-dlq"
  message_retention_seconds  = 1209600
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 60
}

resource "aws_sqs_queue" "cowork" {
  name                       = var.name_prefix
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 60
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.cowork_dlq.arn
    maxReceiveCount     = 5
  })
}

data "aws_iam_policy_document" "sqs_from_s3" {
  statement {
    sid     = "AllowS3Notify"
    actions = ["sqs:SendMessage"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    resources = [aws_sqs_queue.cowork.arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:s3:::${var.audit_bucket}"]
    }
  }
}

resource "aws_sqs_queue_policy" "cowork" {
  queue_url = aws_sqs_queue.cowork.id
  policy    = data.aws_iam_policy_document.sqs_from_s3.json
}

# Notify configuration is owned by the parent stack's bucket — operators
# add a queue notification with filter_prefix = "${var.cowork_prefix}/"
# to that bucket via terraform/aws/main.tf (or out-of-band). We DO NOT
# create the notification here because aws_s3_bucket_notification is
# REPLACED on each apply — managing it from two stacks would conflict.

# ─── Cross-account access for XSIAM (re-uses parent stack's external_id) ──
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
      values   = [var.xsiam_external_id]
    }
  }
}

resource "aws_iam_role" "xsiam" {
  name               = "${var.name_prefix}-xsiam-ingest"
  assume_role_policy = data.aws_iam_policy_document.xsiam_assume.json
}

data "aws_iam_policy_document" "xsiam" {
  statement {
    sid       = "ReadCoworkObjects"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.audit_bucket}/${var.cowork_prefix}/*"]
  }
  statement {
    sid       = "ListBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${var.audit_bucket}"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.cowork_prefix}/*"]
    }
  }
  statement {
    sid = "ConsumeNotifications"
    actions = [
      "sqs:ReceiveMessage", "sqs:DeleteMessage",
      "sqs:GetQueueAttributes", "sqs:GetQueueUrl",
    ]
    resources = [aws_sqs_queue.cowork.arn]
  }
}

resource "aws_iam_role_policy" "xsiam" {
  role   = aws_iam_role.xsiam.id
  policy = data.aws_iam_policy_document.xsiam.json
}

# ─── ECS Fargate cluster + task ───────────────────────────────────────────
resource "aws_ecs_cluster" "this" {
  name = var.name_prefix
}

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${var.name_prefix}-exec"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Execution role also needs to read the secrets at container start.
data "aws_iam_policy_document" "task_execution_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.bearer_token.arn,
      aws_secretsmanager_secret.collector_config.arn,
    ]
  }
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.task_execution_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

# Runtime permissions: write to the audit bucket under cowork/ prefix.
data "aws_iam_policy_document" "task" {
  statement {
    sid       = "WriteCoworkObjects"
    actions   = ["s3:PutObject"]
    resources = ["arn:aws:s3:::${var.audit_bucket}/${var.cowork_prefix}/*"]
  }
}

resource "aws_iam_role_policy" "task" {
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

resource "aws_cloudwatch_log_group" "task" {
  name              = "/ecs/${var.name_prefix}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "this" {
  family                   = var.name_prefix
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "otelcol"
      image     = var.collector_image
      essential = true
      portMappings = [
        { containerPort = 4318, protocol = "tcp" }, # OTLP HTTP
        { containerPort = 4317, protocol = "tcp" }, # OTLP gRPC
        { containerPort = 13133, protocol = "tcp" } # health check
      ]
      command = ["--config=env:COLLECTOR_CONFIG"]
      secrets = [
        { name = "OTEL_AUTH_TOKEN", valueFrom = aws_secretsmanager_secret.bearer_token.arn },
        { name = "COLLECTOR_CONFIG", valueFrom = aws_secretsmanager_secret.collector_config.arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.task.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "otelcol"
        }
      }
      healthCheck = {
        command  = ["CMD-SHELL", "wget -qO- http://localhost:13133/ || exit 1"]
        interval = 30
        timeout  = 5
        retries  = 3
      }
    }
  ])
}

# ─── ALB (HTTPS termination) ──────────────────────────────────────────────
resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "Public HTTPS for OTel collector."
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTPS from anywhere (Cowork backend + workstations)."
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "task" {
  name        = "${var.name_prefix}-task"
  description = "Fargate task — only ALB can reach it."
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 4318
    to_port         = 4318
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    from_port       = 13133
    to_port         = 13133
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "this" {
  name               = var.name_prefix
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids
}

resource "aws_lb_target_group" "this" {
  name        = var.name_prefix
  port        = 4318
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  health_check {
    enabled             = true
    path                = "/"
    port                = "13133"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

resource "aws_ecs_service" "this" {
  name            = var.name_prefix
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.task_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true # required so Fargate can reach S3 / ECR
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = "otelcol"
    container_port   = 4318
  }

  depends_on = [aws_lb_listener.https]
}
