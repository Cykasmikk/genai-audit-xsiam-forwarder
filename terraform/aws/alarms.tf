# Optional CloudWatch alarms for the polling forwarder.
#
# Set var.pager_sns_topic_arn to wire up alarm notifications. When unset,
# alarms are still created but have no actions configured — useful for
# OK-Insufficient-data review during a drill.

variable "pager_sns_topic_arn" {
  description = <<-EOT
    SNS topic ARN to notify on alarm transitions. Leave empty to create
    alarms without action handlers (visible in the console only).
  EOT
  type        = string
  default     = ""
}

variable "stale_invocation_threshold_minutes" {
  description = <<-EOT
    Alarm if a feed has had zero successful Lambda invocations for this
    many minutes. Default 30 = 6 ticks at the default 5-min schedule.
  EOT
  type        = number
  default     = 30
}

# ─── Per-feed: any error within a 5-min window pages immediately ──────────
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = var.vendors
  alarm_name          = "${var.name_prefix}-${each.key}-errors"
  alarm_description   = "Lambda errors > 0 in 5 min for ${each.key} feed."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.forwarder[each.key].function_name
  }

  alarm_actions = var.pager_sns_topic_arn == "" ? [] : [var.pager_sns_topic_arn]
  ok_actions    = var.pager_sns_topic_arn == "" ? [] : [var.pager_sns_topic_arn]
}

# ─── Per-feed: silent forwarder (no successful invocation) → ticket ───────
resource "aws_cloudwatch_metric_alarm" "lambda_stale" {
  for_each            = var.vendors
  alarm_name          = "${var.name_prefix}-${each.key}-stale"
  alarm_description   = "${each.key} feed had no successful invocation in ${var.stale_invocation_threshold_minutes} min — possible silent forwarder failure."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = ceil(var.stale_invocation_threshold_minutes / 5)
  datapoints_to_alarm = ceil(var.stale_invocation_threshold_minutes / 5)
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "breaching"

  dimensions = {
    FunctionName = aws_lambda_function.forwarder[each.key].function_name
  }

  alarm_actions = var.pager_sns_topic_arn == "" ? [] : [var.pager_sns_topic_arn]
  ok_actions    = var.pager_sns_topic_arn == "" ? [] : [var.pager_sns_topic_arn]
}

# ─── Per-feed: DLQ has messages → things have been redriven → investigate ─
resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  for_each            = var.vendors
  alarm_name          = "${var.name_prefix}-${each.key}-dlq-depth"
  alarm_description   = "DLQ for ${each.key} has un-processed messages — XSIAM-side issue or repeated egress failure."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.audit_dlq[each.key].name
  }

  alarm_actions = var.pager_sns_topic_arn == "" ? [] : [var.pager_sns_topic_arn]
}
