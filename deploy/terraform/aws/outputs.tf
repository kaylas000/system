output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "configure_kubectl" {
  value = "aws eks update-kubeconfig --region ${var.aws_region} --name ${module.eks.cluster_name}"
}

output "rds_endpoint" {
  value = aws_db_instance.postgres.endpoint
}

output "redis_primary_endpoint" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "kernel_secret_arn" {
  value = aws_secretsmanager_secret.kernel.arn
}

output "kernel_irsa_role_arn" {
  description = "Set as serviceAccount.annotations.eks.amazonaws.com/role-arn in the Helm values."
  value       = aws_iam_role.kernel.arn
}
