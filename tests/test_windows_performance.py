from __future__ import annotations

import windows_performance as wp


def test_process_group_impact_scores_memory_cpu_and_counts() -> None:
    processes = [
        {"pid": 1, "name": "browser.exe", "rss_mb": 700.0, "cpu_percent": 12.0, "threads": 20},
        {"pid": 2, "name": "browser.exe", "rss_mb": 500.0, "cpu_percent": 4.0, "threads": 10},
        {"pid": 3, "name": "tiny.exe", "rss_mb": 10.0, "cpu_percent": 0.0, "threads": 1},
    ]

    groups = wp.process_group_impact(processes, total_memory_mb=8_000, limit=10)

    assert groups[0]["name"] == "browser.exe"
    assert groups[0]["rss_mb"] == 1200.0
    assert groups[0]["cpu_percent"] == 16.0
    assert groups[0]["impact_score"] >= 4
    assert groups[0]["approval_required"] is True


def test_startup_impact_marks_review_only_actions() -> None:
    rows = wp.startup_impact(
        [
            {
                "Name": "VendorUpdater",
                "Command": "C:/Program Files/Vendor/updater.exe",
                "Location": "HKCU/Software/Microsoft/Windows/CurrentVersion/Run",
            }
        ]
    )

    assert rows[0]["name"] == "VendorUpdater"
    assert rows[0]["approval_required"] is True
    assert "disabling" in rows[0]["safe_action"]


def test_safe_recommendations_detects_pressure_and_network_failures() -> None:
    snapshot = {
        "counters": {
            "memory": {"percent": 90},
            "cpu": {"overall_percent": 81},
        },
        "latency": {"targets": [{"name": "homeassistant", "success_percent": 50}]},
        "process_groups": [{"name": "app.exe", "impact_score": 4, "safe_action": "review only"}],
    }

    recommendations = wp.safe_recommendations(snapshot)

    assert {row["area"] for row in recommendations} == {"memory", "cpu", "network", "process"}
    assert all(row["approval_required"] is True for row in recommendations)


def test_summarize_durations_handles_empty_and_p95() -> None:
    assert wp.summarize_durations([])["count"] == 0

    summary = wp.summarize_durations([{"duration_ms": 10}, {"duration_ms": 20}, {"duration_ms": 100}])

    assert summary["avg_ms"] == 43.3
    assert summary["p95_ms"] == 100.0
