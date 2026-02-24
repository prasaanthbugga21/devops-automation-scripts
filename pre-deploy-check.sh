#!/usr/bin/env bash
# =============================================================================
# pre-deploy-check.sh — Pre-deployment readiness checker for EKS
#
# Validates cluster health, node readiness, and PDB availability before
# allowing a deployment to proceed. Designed to run as the first step in the
# CI/CD pipeline's production deploy job.
#
# Usage:
#   bash pre-deploy-check.sh --cluster prod-eks-cluster --namespace production
#   bash pre-deploy-check.sh --cluster prod-eks-cluster --namespace production --strict
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
CLUSTER=""
NAMESPACE="production"
REGION="us-east-1"
STRICT_MODE=false
MIN_READY_NODES=2
MAX_NOT_READY_PODS=5

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_pass()  { echo -e "${GREEN}  [✓] $1${NC}"; }
log_fail()  { echo -e "${RED}  [✗] $1${NC}"; }
log_warn()  { echo -e "${YELLOW}  [!] $1${NC}"; }
log_info()  { echo -e "${BLUE}  [-] $1${NC}"; }

PASS=0; FAIL=0; WARN=0

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --cluster)   CLUSTER="$2"; shift ;;
    --namespace) NAMESPACE="$2"; shift ;;
    --region)    REGION="$2"; shift ;;
    --strict)    STRICT_MODE=true ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
  shift
done

[[ -z "$CLUSTER" ]] && { echo "ERROR: --cluster is required"; exit 1; }

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
echo ""
echo "======================================================"
echo "  Pre-Deployment Readiness Check"
echo "  Cluster  : ${CLUSTER}"
echo "  Namespace: ${NAMESPACE}"
echo "  Region   : ${REGION}"
echo "  Strict   : ${STRICT_MODE}"
echo "======================================================"

log_info "Updating kubeconfig..."
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" --quiet

# ---------------------------------------------------------------------------
# CHECK 1: Node readiness
# ---------------------------------------------------------------------------
echo ""
echo "--- Node Health ---"
TOTAL_NODES=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
READY_NODES=$(kubectl get nodes --no-headers 2>/dev/null | grep -c " Ready " || echo "0")
NOT_READY_NODES=$(( TOTAL_NODES - READY_NODES ))

log_info "Nodes: ${READY_NODES}/${TOTAL_NODES} ready"

if [ "$READY_NODES" -lt "$MIN_READY_NODES" ]; then
  log_fail "Fewer than ${MIN_READY_NODES} nodes are Ready (found: ${READY_NODES})"
  kubectl get nodes --no-headers
  ((FAIL++))
elif [ "$NOT_READY_NODES" -gt 0 ]; then
  log_warn "${NOT_READY_NODES} node(s) are NOT Ready — review before deploying"
  ((WARN++))
else
  log_pass "All ${READY_NODES} nodes are Ready"
  ((PASS++))
fi

# ---------------------------------------------------------------------------
# CHECK 2: Pod health in target namespace
# ---------------------------------------------------------------------------
echo ""
echo "--- Pod Health (namespace: ${NAMESPACE}) ---"

if ! kubectl get namespace "$NAMESPACE" &>/dev/null; then
  log_warn "Namespace '${NAMESPACE}' does not exist yet — will be created by Helm"
  ((WARN++))
else
  NOT_RUNNING=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
    | grep -v -E "Running|Completed|Terminating" | wc -l || echo "0")

  if [ "$NOT_RUNNING" -gt "$MAX_NOT_READY_PODS" ]; then
    log_fail "${NOT_RUNNING} pods in namespace '${NAMESPACE}' are not in Running state"
    kubectl get pods -n "$NAMESPACE" --no-headers | grep -v -E "Running|Completed"
    ((FAIL++))
  elif [ "$NOT_RUNNING" -gt 0 ]; then
    log_warn "${NOT_RUNNING} pod(s) in namespace '${NAMESPACE}' are not Running"
    ((WARN++))
  else
    log_pass "All pods in namespace '${NAMESPACE}' are healthy"
    ((PASS++))
  fi
fi

# ---------------------------------------------------------------------------
# CHECK 3: PodDisruptionBudget availability
# ---------------------------------------------------------------------------
echo ""
echo "--- PodDisruptionBudget Check ---"
PDB_DATA=$(kubectl get pdb -n "$NAMESPACE" --no-headers 2>/dev/null || echo "")

if [ -z "$PDB_DATA" ]; then
  log_warn "No PodDisruptionBudgets found in namespace '${NAMESPACE}'"
  ((WARN++))
else
  DISRUPTED_PDBS=$(echo "$PDB_DATA" | awk '{if ($4 == 0) print $0}' | wc -l || echo "0")
  if [ "$DISRUPTED_PDBS" -gt 0 ]; then
    log_fail "${DISRUPTED_PDBS} PDB(s) show 0 allowed disruptions — unsafe to deploy"
    echo "$PDB_DATA"
    ((FAIL++))
  else
    log_pass "All PDBs have available disruption budget"
    ((PASS++))
  fi
fi

# ---------------------------------------------------------------------------
# CHECK 4: Recent cluster events (warnings)
# ---------------------------------------------------------------------------
echo ""
echo "--- Recent Cluster Warning Events (last 10 min) ---"
WARNINGS=$(kubectl get events -n "$NAMESPACE" --field-selector type=Warning \
  --no-headers 2>/dev/null | tail -5 || echo "")

if [ -n "$WARNINGS" ]; then
  log_warn "Recent Warning events detected:"
  echo "$WARNINGS"
  ((WARN++))
else
  log_pass "No recent Warning events in namespace '${NAMESPACE}'"
  ((PASS++))
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "======================================================"
echo "  Summary: ${PASS} passed | ${WARN} warnings | ${FAIL} failed"
echo "======================================================"

if [ "$FAIL" -gt 0 ]; then
  echo -e "${RED}Pre-deployment check FAILED. Blocking deployment.${NC}"
  exit 1
fi

if [ "$STRICT_MODE" = true ] && [ "$WARN" -gt 0 ]; then
  echo -e "${RED}Strict mode: warnings treated as failures. Blocking deployment.${NC}"
  exit 1
fi

echo -e "${GREEN}Pre-deployment checks passed. Proceeding with deployment.${NC}"
