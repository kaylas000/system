# specs/06_ops/deployment/TERRAFORM/main.tf
# Terraform Module for AWS Cloud Infrastructure

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.23" }
    helm = { source = "hashicorp/helm", version = "~> 2.10" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "s3" {
    bucket = "autogen-terraform-state"
    key    = "prod/infra/terraform.tfstate"
    region = "us-east-1"
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = "autogen"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# --- VPC & Networking ---
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"
  name    = "${var.project_name}-${var.environment}"
  cidr    = "10.0.0.0/16"
  azs             = ["${var.aws_region}a", "${var.aws_region}b", "${var.aws_region}c"]
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]
  enable_nat_gateway     = true
  single_nat_gateway     = var.environment != "prod"
  enable_dns_hostnames   = true
  enable_dns_support     = true
}

# --- EKS Cluster ---
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 19.0"
  cluster_name    = "${var.project_name}-${var.environment}"
  cluster_version = "1.28"
  vpc_id          = module.vpc.vpc_id
  subnet_ids      = module.vpc.private_subnets
  cluster_endpoint_public_access = true
  
  eks_managed_node_group_defaults = {
    ami_type       = "AL2_x86_64"
    instance_types = ["m6i.xlarge"]
  }

  eks_managed_node_groups = {
    general = {
      name           = "general"
      instance_types = ["m6i.xlarge", "m6i.2xlarge"]
      capacity_type  = "ON_DEMAND"
      min_size       = 3
      max_size       = 20
      desired_size   = 3
      labels = { workload = "general" }
    }
    sandbox = {
      name           = "sandbox"
      instance_types = ["c6i.2xlarge", "c6i.4xlarge"] # CPU optimized for code execution
      capacity_type  = "SPOT"
      min_size       = 0
      max_size       = 10
      desired_size   = 0
      labels = { workload = "sandbox" }
      taints = [{ key = "workload", value = "sandbox", effect = "NO_SCHEDULE" }]
    }
  }
}

# --- RDS PostgreSQL (State & Metadata) ---
module "rds" {
  source  = "terraform-aws-modules/rds/aws"
  version = "~> 6.0"
  identifier = "${var.project_name}-${var.environment}-postgres"
  engine               = "postgres"
  engine_version       = "16.3"
  instance_class       = "db.r6g.xlarge"
  allocated_storage    = 100
  max_allocated_storage = 500
  storage_encrypted    = true
  db_name              = "autogen"
  username             = "autogen_admin"
  password             = random_password.db.result
  vpc_id               = module.vpc.vpc_id
  subnet_ids           = module.vpc.private_subnets
  security_groups      = [module.rds_sg.id]
  backup_retention_period = 7
  deletion_protection  = var.environment == "prod"
}

# --- ElastiCache Redis (Caching, Queue, Rate Limit) ---
module "redis" {
  source  = "terraform-aws-modules/elasticache/aws"
  version = "~> 2.0"
  cluster_id           = "${var.project_name}-${var.environment}-redis"
  engine               = "redis"
  node_type            = "cache.r6g.xlarge"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  subnet_group_name    = module.elasticache_subnet_group.name
  security_group_ids   = [module.redis_sg.id]
  at_rest_encryption   = true
  transit_encryption   = true
  auth_token           = random_password.redis.result
}

# --- S3 for Artifacts & Knowledge Base ---
resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.project_name}-${var.environment}-artifacts"
  server_side_encryption_configuration {
    rule { apply_server_side_encryption_by_default { sse_algorithm = "AES256" } }
  }
  versioning { enabled = true }
  lifecycle_rule {
    enabled = true
    expiration { days = 90 }
    noncurrent_version_expiration { days = 30 }
  }
}

# --- IAM Roles for Service Accounts (IRSA) ---
module "irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"
  role_map = {
    autogen-kernel = {
      role_name = "autogen-kernel-${var.environment}"
      attach_policies = [
        "arn:aws:iam::aws:policy/SecretsManagerReadWrite",
        "arn:aws:iam::aws:policy/AmazonS3FullAccess", # For artifacts
      ]
      oidc_providers = {
        main = { provider_arn = module.eks.oidc_provider_arn, namespace_service_accounts = ["default:autogen-kernel"] }
      }
    }
  }
}

# --- Secrets ---
resource "random_password" "db" { length = 32, special = false }
resource "random_password" "redis" { length = 32, special = false }

resource "aws_secretsmanager_secret" "db" { name = "/autogen/${var.environment}/database-url" }
resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    host = module.rds.db_instance_endpoint
    port = 5432
    username = "autogen_admin"
    password = random_password.db.result
    dbname = "autogen"
  })
}

# --- Outputs ---
output "kubeconfig" {
  value = module.eks.kubeconfig
  sensitive = true
}
output "rds_endpoint" { value = module.rds.db_instance_endpoint }
output "redis_endpoint" { value = module.redis.elasticache_replication_group_primary_endpoint_address }
