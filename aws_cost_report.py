#!/usr/bin/env python3
"""
aws_cost_report.py — Automated AWS cost analysis and anomaly detection.

Queries AWS Cost Explorer for the current month's spend by service,
compares against the previous month, flags cost anomalies, and outputs
a formatted report. Designed to be run as a scheduled Lambda or cron job.

Usage:
    python3 aws_cost_report.py --profile default --region us-east-1
    python3 aws_cost_report.py --slack-webhook https://hooks.slack.com/...
"""

import argparse
import json
import logging
import os
from datetime import datetime, timedelta

import boto3
import requests

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cost Explorer queries
# ---------------------------------------------------------------------------
def get_monthly_cost_by_service(ce_client, start: str, end: str) -> dict[str, float]:
    """Retrieve total cost grouped by AWS service for a given date range."""
    response = ce_client.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    costs = {}
    for group in response["ResultsByTime"][0].get("Groups", []):
        service = group["Keys"][0]
        amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
        if amount > 0.01:  # Filter out rounding noise
            costs[service] = round(amount, 2)

    return dict(sorted(costs.items(), key=lambda x: x[1], reverse=True))


def detect_anomalies(current: dict, previous: dict, threshold_pct: float = 25.0) -> list[dict]:
    """Flag services with cost increases exceeding the threshold percentage."""
    anomalies = []
    for service, current_cost in current.items():
        previous_cost = previous.get(service, 0)
        if previous_cost == 0:
            if current_cost > 10:  # New service with meaningful spend
                anomalies.append({
                    "service": service,
                    "current": current_cost,
                    "previous": previous_cost,
                    "change_pct": 100.0,
                    "reason": "New service with spend this month",
                })
            continue

        change_pct = ((current_cost - previous_cost) / previous_cost) * 100
        if change_pct >= threshold_pct:
            anomalies.append({
                "service": service,
                "current": current_cost,
                "previous": previous_cost,
                "change_pct": round(change_pct, 1),
                "reason": f"Cost increased by {change_pct:.1f}% vs last month",
            })

    return sorted(anomalies, key=lambda x: x["change_pct"], reverse=True)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_console_report(
    current_costs: dict,
    previous_costs: dict,
    anomalies: list,
    current_period: tuple,
    previous_period: tuple,
) -> str:
    """Format a human-readable cost report for console output."""
    total_current = sum(current_costs.values())
    total_previous = sum(previous_costs.values())
    total_change = total_current - total_previous
    total_change_pct = ((total_change / total_previous) * 100) if total_previous else 0

    lines = [
        "",
        "=" * 65,
        "  AWS Monthly Cost Report",
        f"  Current period : {current_period[0]} → {current_period[1]}",
        f"  Previous period: {previous_period[0]} → {previous_period[1]}",
        "=" * 65,
        f"  Total (current) : ${total_current:,.2f}",
        f"  Total (previous): ${total_previous:,.2f}",
        f"  Change          : ${total_change:+,.2f} ({total_change_pct:+.1f}%)",
        "=" * 65,
        "",
        "  Top Services by Cost:",
        "",
    ]

    for service, cost in list(current_costs.items())[:15]:
        prev = previous_costs.get(service, 0)
        change = cost - prev
        lines.append(f"  {service:<45} ${cost:>8.2f}  ({change:+.2f})")

    if anomalies:
        lines += [
            "",
            f"  ⚠  Cost Anomalies Detected ({len(anomalies)}):",
            "",
        ]
        for a in anomalies:
            lines.append(
                f"  ! {a['service']:<43} ${a['current']:>8.2f}  (+{a['change_pct']}%)"
            )

    lines.append("=" * 65)
    return "\n".join(lines)


def post_to_slack(webhook_url: str, total: float, anomalies: list, period: tuple) -> None:
    """Post a cost summary and anomaly alert to a Slack channel."""
    if not webhook_url:
        return

    anomaly_text = ""
    if anomalies:
        anomaly_lines = [f"• {a['service']}: ${a['current']:.2f} (+{a['change_pct']}%)" for a in anomalies[:5]]
        anomaly_text = "\n\n*⚠ Cost Anomalies:*\n" + "\n".join(anomaly_lines)

    payload = {
        "text": (
            f"*AWS Cost Report — {period[0]} to {period[1]}*\n"
            f"Total spend this period: *${total:,.2f}*"
            f"{anomaly_text}"
        )
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Slack notification sent successfully.")
    except requests.RequestException as e:
        log.error("Failed to send Slack notification: %s", e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AWS cost analysis and anomaly detection")
    parser.add_argument("--profile", default=None, help="AWS CLI profile")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument("--anomaly-threshold", type=float, default=25.0, help="% increase to flag as anomaly")
    parser.add_argument("--slack-webhook", default=os.getenv("SLACK_WEBHOOK_URL"), help="Slack webhook URL")
    return parser.parse_args()


def main():
    args = parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ce = session.client("ce", region_name="us-east-1")  # Cost Explorer is global, us-east-1 endpoint

    today = datetime.utcnow().date()
    first_day_current = today.replace(day=1)
    first_day_previous = (first_day_current - timedelta(days=1)).replace(day=1)
    last_day_previous = first_day_current - timedelta(days=1)

    current_start = str(first_day_current)
    current_end = str(today)
    prev_start = str(first_day_previous)
    prev_end = str(last_day_previous)

    log.info("Fetching current period costs: %s → %s", current_start, current_end)
    current_costs = get_monthly_cost_by_service(ce, current_start, current_end)

    log.info("Fetching previous period costs: %s → %s", prev_start, prev_end)
    previous_costs = get_monthly_cost_by_service(ce, prev_start, prev_end)

    anomalies = detect_anomalies(current_costs, previous_costs, args.anomaly_threshold)

    report = format_console_report(
        current_costs,
        previous_costs,
        anomalies,
        (current_start, current_end),
        (prev_start, prev_end),
    )
    print(report)

    total = sum(current_costs.values())
    post_to_slack(args.slack_webhook, total, anomalies, (current_start, current_end))

    if anomalies:
        log.warning("%d cost anomalies detected. Review report above.", len(anomalies))


if __name__ == "__main__":
    main()
