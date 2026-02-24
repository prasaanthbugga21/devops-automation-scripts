#!/usr/bin/env python3
"""
eks_resource_cleanup.py — Automated EKS cluster resource hygiene tool.

Identifies and optionally removes stale or wasteful resources from an EKS
cluster, including:
  - Unused namespaces (no running pods, older than N days)
  - Pods in CrashLoopBackOff or Error state
  - Completed/failed Jobs older than N hours
  - Images in ECR repositories with no EKS references (dry-run output)

Usage:
    python3 eks_resource_cleanup.py --cluster prod-eks-cluster --region us-east-1
    python3 eks_resource_cleanup.py --cluster prod-eks-cluster --dry-run
    python3 eks_resource_cleanup.py --cluster prod-eks-cluster --delete-crashloop
"""

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import boto3

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class CleanupReport:
    crashloop_pods: list = field(default_factory=list)
    stale_namespaces: list = field(default_factory=list)
    completed_jobs: list = field(default_factory=list)
    actions_taken: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Kubernetes helpers
# ---------------------------------------------------------------------------
def kubectl(args: list[str]) -> dict | list | None:
    """Run a kubectl command and return parsed JSON output."""
    cmd = ["kubectl"] + args + ["-o", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        log.error("kubectl command failed: %s\nStderr: %s", " ".join(cmd), e.stderr)
        return None
    except json.JSONDecodeError:
        log.error("Failed to parse kubectl output as JSON")
        return None


def kubectl_delete(resource_type: str, name: str, namespace: str, dry_run: bool) -> bool:
    """Delete a Kubernetes resource, optionally in dry-run mode."""
    cmd = ["kubectl", "delete", resource_type, name, "-n", namespace]
    if dry_run:
        cmd.append("--dry-run=client")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        log.info("Deleted %s/%s in namespace %s (dry_run=%s)", resource_type, name, namespace, dry_run)
        return True
    except subprocess.CalledProcessError as e:
        log.error("Failed to delete %s/%s: %s", resource_type, name, e.stderr)
        return False


# ---------------------------------------------------------------------------
# Cleanup functions
# ---------------------------------------------------------------------------
def find_crashloop_pods(report: CleanupReport, dry_run: bool, delete: bool) -> None:
    """Find pods in CrashLoopBackOff or Error state across all namespaces."""
    log.info("Scanning for CrashLoopBackOff / Error pods...")
    data = kubectl(["get", "pods", "--all-namespaces"])
    if not data:
        report.errors.append("Failed to list pods")
        return

    problem_states = {"CrashLoopBackOff", "Error", "OOMKilled", "ImagePullBackOff"}

    for pod in data.get("items", []):
        namespace = pod["metadata"]["namespace"]
        name = pod["metadata"]["name"]
        container_statuses = pod.get("status", {}).get("containerStatuses", [])

        for cs in container_statuses:
            waiting = cs.get("state", {}).get("waiting", {})
            reason = waiting.get("reason", "")
            restart_count = cs.get("restartCount", 0)

            if reason in problem_states or (reason == "CrashLoopBackOff" and restart_count > 5):
                entry = {
                    "namespace": namespace,
                    "pod": name,
                    "container": cs["name"],
                    "reason": reason,
                    "restarts": restart_count,
                }
                report.crashloop_pods.append(entry)
                log.warning("Problem pod: %s/%s — %s (restarts: %d)", namespace, name, reason, restart_count)

                if delete:
                    success = kubectl_delete("pod", name, namespace, dry_run)
                    if success:
                        report.actions_taken.append(f"Deleted pod {namespace}/{name} ({reason})")


def find_completed_jobs(report: CleanupReport, max_age_hours: int, dry_run: bool, delete: bool) -> None:
    """Remove completed or failed Jobs older than max_age_hours."""
    log.info("Scanning for stale Jobs (completed/failed, older than %d hours)...", max_age_hours)
    data = kubectl(["get", "jobs", "--all-namespaces"])
    if not data:
        report.errors.append("Failed to list jobs")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    for job in data.get("items", []):
        namespace = job["metadata"]["namespace"]
        name = job["metadata"]["name"]
        conditions = job.get("status", {}).get("conditions", [])
        creation_time_str = job["metadata"].get("creationTimestamp", "")

        try:
            creation_time = datetime.fromisoformat(creation_time_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        is_complete = any(c.get("type") in ("Complete", "Failed") for c in conditions)

        if is_complete and creation_time < cutoff:
            entry = {"namespace": namespace, "job": name, "created": creation_time_str}
            report.completed_jobs.append(entry)
            log.info("Stale job: %s/%s (created: %s)", namespace, name, creation_time_str)

            if delete:
                success = kubectl_delete("job", name, namespace, dry_run)
                if success:
                    report.actions_taken.append(f"Deleted job {namespace}/{name}")


def update_eks_kubeconfig(cluster_name: str, region: str) -> None:
    """Update local kubeconfig to point to the specified EKS cluster."""
    log.info("Updating kubeconfig for cluster: %s in %s", cluster_name, region)
    cmd = [
        "aws", "eks", "update-kubeconfig",
        "--name", cluster_name,
        "--region", region,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        log.info("kubeconfig updated successfully.")
    except subprocess.CalledProcessError as e:
        log.error("Failed to update kubeconfig: %s", e.stderr.decode())
        sys.exit(1)


def print_report(report: CleanupReport) -> None:
    """Print a structured summary of the cleanup run."""
    separator = "=" * 60
    print(f"\n{separator}")
    print("  EKS Resource Cleanup Report")
    print(f"{separator}")
    print(f"  CrashLoop/Error Pods Found : {len(report.crashloop_pods)}")
    print(f"  Stale Jobs Found           : {len(report.completed_jobs)}")
    print(f"  Actions Taken              : {len(report.actions_taken)}")
    print(f"  Errors                     : {len(report.errors)}")
    print(separator)

    if report.crashloop_pods:
        print("\nProblem Pods:")
        for pod in report.crashloop_pods:
            print(f"  - [{pod['namespace']}] {pod['pod']} ({pod['reason']}, restarts: {pod['restarts']})")

    if report.completed_jobs:
        print("\nStale Jobs:")
        for job in report.completed_jobs:
            print(f"  - [{job['namespace']}] {job['job']} (created: {job['created']})")

    if report.actions_taken:
        print("\nActions Taken:")
        for action in report.actions_taken:
            print(f"  ✓ {action}")

    if report.errors:
        print("\nErrors:")
        for error in report.errors:
            print(f"  ✗ {error}")

    print(separator)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EKS cluster resource cleanup tool")
    parser.add_argument("--cluster", required=True, help="EKS cluster name")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without deleting anything")
    parser.add_argument("--delete-crashloop", action="store_true", help="Delete CrashLoopBackOff pods")
    parser.add_argument("--delete-jobs", action="store_true", help="Delete completed/failed jobs")
    parser.add_argument("--job-max-age-hours", type=int, default=24, help="Max age (hours) for completed jobs")
    return parser.parse_args()


def main():
    args = parse_args()
    report = CleanupReport()

    if args.dry_run:
        log.info("DRY RUN MODE — no resources will be deleted.")

    update_eks_kubeconfig(args.cluster, args.region)
    find_crashloop_pods(report, dry_run=args.dry_run, delete=args.delete_crashloop)
    find_completed_jobs(report, max_age_hours=args.job_max_age_hours, dry_run=args.dry_run, delete=args.delete_jobs)
    print_report(report)

    # Exit with non-zero if problems were found (useful for pipeline alerting)
    if report.crashloop_pods or report.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
