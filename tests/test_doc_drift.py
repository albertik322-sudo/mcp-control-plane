from __future__ import annotations

import doc_drift


def test_retired_or_disabled_units_are_not_expected_live() -> None:
    inventory = doc_drift.extract_doc_inventory(
        {
            "INFRASTRUCTURE.md": """
## Systemd On Home Server
| `active-agent.service` | Active service. |
| `retired-agent.service` | Retired and disabled. |

### DCV rollback
Re-enable and start `dcvserver.service` only when DCV is needed again.
"""
        }
    )

    assert "active-agent.service" in inventory["home_systemd_units"]
    assert "retired-agent.service" not in inventory["home_systemd_units"]
    assert "dcvserver.service" not in inventory["home_systemd_units"]

    findings = doc_drift.compare_inventory(
        inventory,
        {"containers": [], "systemd_units": ["active-agent.service"], "monitor_domains": [], "compose_paths": []},
    )
    assert not [finding for finding in findings if finding["area"] == "systemd"]


def test_external_vps_unit_blocks_are_not_home_server_expectations() -> None:
    inventory = doc_drift.extract_doc_inventory(
        {
            "RUNBOOK.md": """
## VPN load
Remote components on every VPS:

    /etc/systemd/system/homelab-vpn-capacity.service
    /etc/systemd/system/homelab-vpn-capacity.timer

Home components:

    homelab-telegram-bot.service
"""
        }
    )

    assert inventory["home_systemd_units"] == ["homelab-telegram-bot.service"]
    assert inventory["systemd_unit_metadata"]["homelab-vpn-capacity.service"]["scopes"] == ["external"]

    findings = doc_drift.compare_inventory(
        inventory,
        {"containers": [], "systemd_units": ["homelab-telegram-bot.service"], "monitor_domains": [], "compose_paths": []},
    )
    assert not [finding for finding in findings if finding["area"] == "systemd"]


def test_real_remote_section_phrases_keep_vps_units_out_of_home_inventory() -> None:
    inventory = doc_drift.extract_doc_inventory(
        {
            "RUNBOOK.md": """
## VPN Observer
External VPN usage and capacity telemetry.
Remote files and units:
    homelab-vpn-observer.service
    vnstat.service

## WireGuard keepalive
Changes applied on `WG` (`5.129.227.247`):
    homelab-wg-keepalive.timer

## Power watchdog
Installed on all remote VPN hosts.
Remote systemd units:
    homelab-power-watchdog.service
    homelab-power-watchdog.timer
"""
        }
    )

    assert inventory["home_systemd_units"] == []
    for unit in (
        "homelab-vpn-observer.service",
        "vnstat.service",
        "homelab-wg-keepalive.timer",
        "homelab-power-watchdog.service",
        "homelab-power-watchdog.timer",
    ):
        assert inventory["systemd_unit_metadata"][unit]["scopes"] == ["external"]


def test_explicit_external_scope_overrides_an_earlier_implicit_home_reference() -> None:
    inventory = doc_drift.extract_doc_inventory(
        {
            "INFRASTRUCTURE.md": """
## VPN telemetry
| Scheduling | homelab-vpn-capacity.timer, once per minute |
| Safe restart | Start the service on a VPS. |
""",
            "RUNBOOK.md": """
## VPN telemetry checks
VPS checks:
    systemctl status homelab-vpn-capacity.timer
""",
        }
    )

    assert "homelab-vpn-capacity.timer" not in inventory["home_systemd_units"]
    assert inventory["systemd_unit_metadata"]["homelab-vpn-capacity.timer"]["scopes"] == ["external", "home"]


def test_monitor_domains_are_parsed_from_urls_without_encoded_path_artifacts() -> None:
    inventory = doc_drift.normalize_live_inventory(
        {
            "prometheus_targets": {
                "data": {
                    "activeTargets": [
                        {
                            "scrapeUrl": (
                                "http://example.com:9115/probe?module=http_2xx&"
                                "target=https%3A%2F%2Fauth.example.com%2Fif%2Fflow"
                            )
                        }
                    ]
                }
            },
            "uptime_kuma": {
                "monitors": [
                    {"url": "https%3A%2F%2Fha.example.lan%2Fstatus"},
                    {"hostname": "/checks/2fpath.example.lan/status"},
                ]
            },
        }
    )

    assert inventory["monitor_domains"] == [
        "auth.example.com",
        "example.com",
        "ha.example.lan",
    ]
    assert not any(domain.startswith("2f") for domain in inventory["monitor_domains"])
