# AWS infrastructure for the kernel (written by the agent from specs/06_ops/deployment/TERRAFORM/main.tf).
# Spec defects fixed (ISSUES O-06): undefined module.rds_sg / module.redis_sg / module.elasticache_subnet_group
# and variables, single-line blocks with two arguments, inline S3 bucket arguments removed in AWS provider v4+,
# `module.eks.kubeconfig` (no such output), IRSA module input `role_map` (does not exist), AWS-managed
# SecretsManagerReadWrite + AmazonS3FullAccess policies (least privilege instead), EKS 1.28 / AL2 AMI (EOL),
# secret named database-url that did not contain a URL.
#
# NOT validated here: `terraform init/validate/plan` need the Terraform registry, unreachable from the
# development environment. Checked: HCL parse + references (tests/ops/test_deploy.py) and checkov.

locals {
  name = "${var.project_name}-${var.environment}"
  azs  = slice(data.aws_availability_zones.available.names, 0, 3)
  prod = var.environment == "prod"
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

# --- VPC ----------------------------------------------------------------------------------------
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.16"

  name            = local.name
  cidr            = var.vpc_cidr
  azs             = local.azs
  private_subnets = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, i + 1)]
  public_subnets  = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, i + 101)]

  enable_nat_gateway   = true
  single_nat_gateway   = !local.prod
  enable_dns_hostnames = true
  enable_dns_support   = true

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}

# --- EKS ----------------------------------------------------------------------------------------
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.31"

  cluster_name                             = local.name
  cluster_version                          = var.cluster_version
  vpc_id                                   = module.vpc.vpc_id
  subnet_ids                               = module.vpc.private_subnets
  cluster_endpoint_public_access           = true
  cluster_endpoint_public_access_cidrs     = var.cluster_endpoint_public_access_cidrs
  enable_cluster_creator_admin_permissions = true
  enable_irsa                              = true

  cluster_addons = {
    coredns                = {}
    kube-proxy             = {}
    vpc-cni                = {}
    eks-pod-identity-agent = {}
    aws-ebs-csi-driver     = { service_account_role_arn = aws_iam_role.ebs_csi.arn }
  }

  eks_managed_node_groups = merge(
    {
      general = {
        ami_type       = "AL2023_x86_64_STANDARD"
        instance_types = var.node_instance_types
        capacity_type  = "ON_DEMAND"
        min_size       = var.node_min_size
        max_size       = var.node_max_size
        desired_size   = var.node_min_size
        labels         = { workload = "general" }
      }
    },
    var.enable_sandbox_nodes ? {
      sandbox = {
        ami_type       = "AL2023_x86_64_STANDARD"
        instance_types = ["c6i.2xlarge", "c6i.4xlarge"]
        capacity_type  = "SPOT"
        min_size       = 0
        max_size       = 10
        desired_size   = 0
        labels         = { workload = "sandbox" }

        taints = {
          sandbox = { key = "workload", value = "sandbox", effect = "NO_SCHEDULE" }
        }
      }
    } : {}
  )
}

# EBS CSI driver (PVC for the kernel /data volume)
data "aws_iam_policy_document" "ebs_csi_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${local.name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_assume.json
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# --- PostgreSQL (LangGraph checkpoints) ---------------------------------------------------------
resource "aws_db_subnet_group" "this" {
  name       = "${local.name}-db"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "PostgreSQL from EKS nodes"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "PostgreSQL from EKS nodes"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_instance" "postgres" {
  identifier                      = "${local.name}-postgres"
  engine                          = "postgres"
  engine_version                  = "16"
  auto_minor_version_upgrade      = true
  instance_class                  = var.db_instance_class
  allocated_storage               = var.db_allocated_storage
  max_allocated_storage           = var.db_allocated_storage * 5
  storage_type                    = "gp3"
  storage_encrypted               = true
  db_name                         = "autogen"
  username                        = "autogen_admin"
  password                        = random_password.db.result
  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  multi_az                        = local.prod
  backup_retention_period         = 7
  deletion_protection             = local.prod
  skip_final_snapshot             = !local.prod
  final_snapshot_identifier       = local.prod ? "${local.name}-postgres-final" : null
  copy_tags_to_snapshot           = true
  performance_insights_enabled    = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
}

# --- Redis (LiteLLM cache, shared rate limits) --------------------------------------------------
resource "aws_elasticache_subnet_group" "this" {
  name       = "${local.name}-redis"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "Redis from EKS nodes"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "Redis from EKS nodes"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }
}

resource "random_password" "redis" {
  length  = 32
  special = false
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${local.name}-redis"
  description                = "AutoGen LiteLLM cache and rate limits"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = local.prod ? 2 : 1
  automatic_failover_enabled = local.prod
  multi_az_enabled           = local.prod
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.redis.result
  snapshot_retention_limit   = local.prod ? 3 : 0
}

# --- S3 artifacts -------------------------------------------------------------------------------
resource "aws_s3_bucket" "artifacts" {
  bucket = "${local.name}-artifacts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-artifacts"
    status = "Enabled"
    filter {}
    expiration {
      days = var.artifact_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# --- Secrets for the kernel (sync into the k8s Secret "autogen-secrets" with External Secrets) ----
resource "aws_secretsmanager_secret" "kernel" {
  name                    = "/${var.project_name}/${var.environment}/kernel"
  recovery_window_in_days = local.prod ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "kernel" {
  secret_id = aws_secretsmanager_secret.kernel.id

  # keys = env var names read by kernel/config.py; LLM / E2B keys are added out of band
  secret_string = jsonencode({
    AUTOGEN_DATABASE__POSTGRES_DSN = "postgresql://autogen_admin:${random_password.db.result}@${aws_db_instance.postgres.endpoint}/autogen?sslmode=require"
    REDIS_URL                      = "rediss://:${random_password.redis.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
  })
  lifecycle {
    ignore_changes = [secret_string] # keys added manually (AUTOGEN_LLM__API_KEY, ...) must survive re-apply
  }
}

# --- IRSA role for the kernel service account (least privilege) ---------------------------------
data "aws_iam_policy_document" "kernel_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:${var.kernel_namespace}:${var.kernel_service_account}"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "kernel" {
  statement {
    sid       = "ArtifactsRW"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }
  statement {
    sid       = "ArtifactsList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
  }
  statement {
    sid       = "KernelSecretRead"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.kernel.arn]
  }
}

resource "aws_iam_role" "kernel" {
  name               = "${local.name}-kernel"
  assume_role_policy = data.aws_iam_policy_document.kernel_assume.json
}

resource "aws_iam_role_policy" "kernel" {
  name   = "kernel"
  role   = aws_iam_role.kernel.id
  policy = data.aws_iam_policy_document.kernel.json
}
