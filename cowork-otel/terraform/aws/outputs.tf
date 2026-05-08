output "collector_endpoint" {
  description = "Public HTTPS endpoint Cowork / Claude Code agents connect to. Append /v1/logs for OTLP HTTP."
  value       = "https://${var.hostname}"
}

output "alb_dns_name" {
  description = "ALB DNS name — point your hostname's CNAME / A-alias here."
  value       = aws_lb.this.dns_name
}

output "bearer_token" {
  description = "Token Cowork / Claude Code agents present in the Authorization header."
  value       = random_password.bearer_token.result
  sensitive   = true
}

output "xsiam_role_arn" {
  description = "Role ARN for the XSIAM data source onboarding (Cowork-specific)."
  value       = aws_iam_role.xsiam.arn
}

output "xsiam_sqs_url" {
  description = "SQS URL for the Cowork S3 generic logs data source."
  value       = aws_sqs_queue.cowork.url
}

output "cowork_queue_arn" {
  description = "Queue ARN to plug into the parent stack's aws_s3_bucket_notification."
  value       = aws_sqs_queue.cowork.arn
}

output "s3_notification_setup" {
  description = <<-EOT
    The shared audit bucket already has S3 notifications managed by the
    parent terraform/aws stack. Add ONE more queue notification to that
    stack with the following settings (using outputs from this stack):

      queue_arn     = <output: cowork_queue_arn>
      events        = ["s3:ObjectCreated:*"]
      filter_prefix = "<your cowork_prefix>/"

    Or do it once via AWS CLI:

      aws s3api put-bucket-notification-configuration \
        --bucket <audit_bucket from parent stack> \
        --notification-configuration ...

    (Avoid managing the same bucket-notification from two Terraform stacks
    — one will clobber the other on each apply.)
  EOT
  value       = aws_sqs_queue.cowork.arn
}
