#!/usr/bin/env bash
# =============================================================================
# ecr-lifecycle-cleanup.sh — Remove untagged and old ECR images
#
# Keeps the N most recent tagged images per repository and deletes untagged
# images. Prevents ECR storage costs from accumulating over time.
# Run weekly via EventBridge Scheduler or GitHub Actions cron.
#
# Usage:
#   bash ecr-lifecycle-cleanup.sh --region us-east-1 --keep 10
#   bash ecr-lifecycle-cleanup.sh --region us-east-1 --repo fastapi-app --keep 5 --dry-run
# =============================================================================

set -euo pipefail

REGION="us-east-1"
KEEP_COUNT=10
TARGET_REPO=""
DRY_RUN=false

while [[ "$#" -gt 0 ]]; do
  case $1 in
    --region)   REGION="$2"; shift ;;
    --keep)     KEEP_COUNT="$2"; shift ;;
    --repo)     TARGET_REPO="$2"; shift ;;
    --dry-run)  DRY_RUN=true ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
  shift
done

echo "========================================"
echo "  ECR Image Lifecycle Cleanup"
echo "  Region  : ${REGION}"
echo "  Keep    : ${KEEP_COUNT} images per repo"
echo "  Dry run : ${DRY_RUN}"
echo "========================================"

TOTAL_DELETED=0
TOTAL_FREED_KB=0

# Get list of repositories
if [ -n "$TARGET_REPO" ]; then
  REPOS=("$TARGET_REPO")
else
  mapfile -t REPOS < <(aws ecr describe-repositories \
    --region "$REGION" \
    --query 'repositories[].repositoryName' \
    --output text | tr '\t' '\n')
fi

for REPO in "${REPOS[@]}"; do
  echo ""
  echo "Processing repository: ${REPO}"

  # --- Delete untagged images ---
  UNTAGGED=$(aws ecr list-images \
    --repository-name "$REPO" \
    --region "$REGION" \
    --filter tagStatus=UNTAGGED \
    --query 'imageIds[*]' \
    --output json 2>/dev/null)

  UNTAGGED_COUNT=$(echo "$UNTAGGED" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

  if [ "$UNTAGGED_COUNT" -gt 0 ]; then
    echo "  Found ${UNTAGGED_COUNT} untagged image(s)"
    if [ "$DRY_RUN" = false ]; then
      aws ecr batch-delete-image \
        --repository-name "$REPO" \
        --region "$REGION" \
        --image-ids "$UNTAGGED" \
        --output json > /dev/null
      echo "  Deleted ${UNTAGGED_COUNT} untagged image(s)"
    else
      echo "  [DRY RUN] Would delete ${UNTAGGED_COUNT} untagged image(s)"
    fi
    TOTAL_DELETED=$(( TOTAL_DELETED + UNTAGGED_COUNT ))
  else
    echo "  No untagged images found"
  fi

  # --- Retain only the N most recent tagged images ---
  ALL_TAGGED=$(aws ecr describe-images \
    --repository-name "$REPO" \
    --region "$REGION" \
    --query 'imageDetails[?imageTags!=`null`]' \
    --output json 2>/dev/null)

  TAGGED_COUNT=$(echo "$ALL_TAGGED" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

  if [ "$TAGGED_COUNT" -gt "$KEEP_COUNT" ]; then
    EXCESS=$(( TAGGED_COUNT - KEEP_COUNT ))
    echo "  ${TAGGED_COUNT} tagged images — removing ${EXCESS} oldest"

    # Get image digests sorted by push date ascending (oldest first)
    TO_DELETE=$(echo "$ALL_TAGGED" | python3 -c "
import json, sys
images = json.load(sys.stdin)
images.sort(key=lambda x: x['imagePushedAt'])
to_delete = images[:${EXCESS}]
result = [{'imageDigest': img['imageDigest']} for img in to_delete]
print(json.dumps(result))
")

    if [ "$DRY_RUN" = false ]; then
      aws ecr batch-delete-image \
        --repository-name "$REPO" \
        --region "$REGION" \
        --image-ids "$TO_DELETE" \
        --output json > /dev/null
      echo "  Deleted ${EXCESS} oldest tagged image(s) from ${REPO}"
    else
      echo "  [DRY RUN] Would delete ${EXCESS} oldest tagged image(s) from ${REPO}"
    fi
    TOTAL_DELETED=$(( TOTAL_DELETED + EXCESS ))
  else
    echo "  ${TAGGED_COUNT} tagged images — within retention limit (${KEEP_COUNT}), no action needed"
  fi
done

echo ""
echo "========================================"
echo "  Cleanup complete"
echo "  Total images removed: ${TOTAL_DELETED}"
echo "========================================"
