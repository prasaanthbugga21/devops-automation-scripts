# devops-automation-scripts

A collection of production-grade DevOps automation scripts used across AWS EKS environments for operational hygiene, cost optimization, and deployment safety.

## Scripts

### Python

| Script | Purpose |
|---|---|
| `python/eks_resource_cleanup.py` | Identifies and removes stale EKS resources: CrashLoopBackOff pods, completed/failed Jobs |
| `python/aws_cost_report.py` | Queries Cost Explorer for monthly spend by service, detects anomalies (25%+ increases), posts to Slack |

### Bash

| Script | Purpose |
|---|---|
| `bash/pre-deploy-check.sh` | Pre-deployment readiness gate: validates node health, pod status, PDB availability before deploying |
| `bash/ecr-lifecycle-cleanup.sh` | Removes untagged ECR images and retains only the N most recent tagged images per repo |
| `bash/smoke-test.sh` | Post-deploy smoke tests validating critical API endpoints are responding correctly |
| `bash/canary-health-check.sh` | Monitors canary deployment for pod restarts and health failures during rollout window |

## Usage Examples

```bash
# Find and delete CrashLoopBackOff pods in production
python3 python/eks_resource_cleanup.py \
  --cluster prod-eks-cluster \
  --region us-east-1 \
  --delete-crashloop

# Preview cleanup without deleting (dry run)
python3 python/eks_resource_cleanup.py \
  --cluster prod-eks-cluster \
  --dry-run

# Generate cost report and post anomalies to Slack
SLACK_WEBHOOK_URL=https://hooks.slack.com/... \
  python3 python/aws_cost_report.py --region us-east-1

# Run pre-deployment readiness check
bash bash/pre-deploy-check.sh \
  --cluster prod-eks-cluster \
  --namespace production \
  --strict

# Clean up ECR images (keep 10 most recent per repo, dry run first)
bash bash/ecr-lifecycle-cleanup.sh \
  --region us-east-1 \
  --keep 10 \
  --dry-run
```

## Design Principles

- **Dry-run first**: Every destructive script supports `--dry-run` to preview changes safely
- **Least privilege**: Scripts assume IAM roles scoped to only the permissions required
- **Structured output**: Python scripts use structured logging; Bash scripts use colored, parseable output
- **Non-zero exit on problems**: Scripts exit with code `1` when issues are found, enabling clean pipeline integration
- **No hardcoded credentials**: All AWS access is via IAM role assumption or instance profiles

## Requirements

```bash
pip install boto3 requests
```

AWS CLI and `kubectl` must be installed and the caller must have appropriate IAM permissions.
