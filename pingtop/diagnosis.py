from __future__ import annotations

import math

from .config import AppConfig
from .models import CheckResult, DiagnosisAssessment


def diagnose_cycle(results: list[CheckResult], config: AppConfig) -> DiagnosisAssessment:
    if not config.targets:
        return DiagnosisAssessment("no_targets", "No targets configured", "no targets configured")
    if not results:
        return DiagnosisAssessment("waiting", "Waiting for first cycle", "waiting for first cycle")

    failures = [result for result in results if result.is_failure]
    if not failures:
        return DiagnosisAssessment(
            "healthy",
            "All monitored targets are reachable",
            "all monitored targets are reachable",
        )

    ip_results = [result for result in results if result.target_type == "ip"]
    host_results = [result for result in results if result.target_type == "hostname"]
    ip_successes = sum(1 for result in ip_results if result.ping_success)
    ip_failures = sum(1 for result in ip_results if not result.ping_success)
    host_dns_failures = sum(1 for result in host_results if result.dns_success is False)
    host_reachability_failures = sum(
        1 for result in host_results if result.dns_success is True and not result.ping_success
    )

    if len(ip_results) >= 2:
        network_threshold = max(2, math.ceil(len(ip_results) * 0.75))
        if ip_failures >= network_threshold:
            return DiagnosisAssessment(
                "network_issue",
                "Likely general network issue",
                "general network issue",
            )

    if len(host_results) >= 2:
        dns_threshold = max(2, math.ceil(len(host_results) * 0.75))
        if ip_successes > 0 and host_dns_failures >= dns_threshold:
            return DiagnosisAssessment(
                "dns_issue",
                "Likely DNS issue",
                "DNS issue",
            )
    if len(failures) == 1 and len(results) > 1:
        return DiagnosisAssessment(
            "isolated_issue",
            "Likely isolated target or path issue",
            "isolated target or path issue",
        )
    if len(host_results) >= 2:
        reachability_threshold = max(2, math.ceil(len(host_results) * 0.75))
        if host_reachability_failures > 0 and host_dns_failures == 0:
            if host_reachability_failures >= reachability_threshold and ip_successes > 0:
                if host_reachability_failures == len(host_results):
                    return DiagnosisAssessment(
                        "host_reachability_all",
                        "DNS okay, but resolved hosts are not reachable",
                        "resolved hosts are not reachable even though DNS is working",
                    )
                return DiagnosisAssessment(
                    "host_reachability_some",
                    "DNS okay, reachability failed for multiple host targets",
                    "host reachability issue after successful DNS resolution",
                )
    return DiagnosisAssessment(
        "mixed_failures",
        "Mixed failures across monitored targets",
        "mixed failures across monitored targets",
    )
