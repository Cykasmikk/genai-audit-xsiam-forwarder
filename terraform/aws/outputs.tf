output "lambda_function_name" {
  value = aws_lambda_function.forwarder.function_name
}

output "log_group" {
  value = aws_cloudwatch_log_group.lambda.name
}

output "state_table" {
  value = aws_dynamodb_table.state.name
}

# ─── Values to paste into the XSIAM "Amazon S3 generic logs" data source ──
output "xsiam_role_arn" {
  description = "IAM role ARN that XSIAM should assume to ingest audit logs."
  value       = aws_iam_role.xsiam.arn
}

output "xsiam_external_id" {
  description = "External ID required for XSIAM to assume the ingest role."
  value       = random_uuid.xsiam_external_id.result
  sensitive   = true
}

output "xsiam_sqs_url" {
  description = "SQS queue URL XSIAM polls for S3 ObjectCreated notifications."
  value       = aws_sqs_queue.audit.url
}

output "audit_bucket" {
  description = "S3 bucket holding gzipped JSON-lines audit objects."
  value       = aws_s3_bucket.audit.bucket
}
