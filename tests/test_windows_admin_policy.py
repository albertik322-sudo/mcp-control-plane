from __future__ import annotations

import pytest

import windows_admin_policy as policy


def test_capabilities_are_allowlisted() -> None:
    caps = policy.capabilities()

    assert "admin_whoami" in caps["actions"]
    assert "raw_powershell" not in caps["actions"]


def test_service_control_rejects_injection() -> None:
    with pytest.raises(ValueError):
        policy.render_action("service_control", {"name": "Spooler; Remove-Item C:\\", "action": "restart"})


def test_service_control_renders_quoted_command() -> None:
    rendered = policy.render_action("service_control", {"name": "Spooler", "action": "restart"}, timeout=9999)

    assert rendered.action == "service_control"
    assert "Restart-Service" in rendered.command
    assert "'Spooler'" in rendered.command
    assert rendered.timeout == 300


def test_persistent_route_add_validates_ipv4() -> None:
    rendered = policy.render_action(
        "persistent_route_add",
        {
            "destination": "192.168.1.10",
            "mask": "255.255.255.255",
            "gateway": "0.0.0.0",
            "interface": 19,
            "metric": 1,
        },
    )

    assert "route -p add 192.168.1.10 mask 255.255.255.255 0.0.0.0 IF 19 metric 1" in rendered.command


def test_wake_device_is_allowlisted_and_checks_programmable_devices() -> None:
    rendered = policy.render_action("enable_wake_device", {"device": "Killer(R) Wi-Fi 6E AX1675x 160MHz Wireless Network Adapter (210NGW)"})

    assert rendered.action == "enable_wake_device"
    assert "wake_programmable" in rendered.command
    assert "/deviceenablewake" in rendered.command
    assert "Killer(R) Wi-Fi 6E AX1675x 160MHz Wireless Network Adapter (210NGW)" in rendered.command


def test_wake_device_rejects_multiline_device_name() -> None:
    with pytest.raises(ValueError):
        policy.render_action("enable_wake_device", {"device": "Killer\nRemove-Item C:\\"})


def test_removable_disk_image_is_bounded_to_codex_recovery() -> None:
    rendered = policy.render_action(
        "removable_disk_image",
        {"disk_index": 2, "output_root": "D:\\CodexRecovery", "chunk_mb": 4},
        timeout=99999,
    )

    assert rendered.action == "removable_disk_image"
    assert "Win32_DiskDrive" in rendered.command
    assert "refusing to image non-removable disk" in rendered.command
    assert "\\\\.\\PhysicalDrive$diskIndex" in rendered.command
    assert "D:\\CodexRecovery" in rendered.command
    assert rendered.timeout == 14400


def test_removable_disk_image_rejects_unbounded_output_path() -> None:
    with pytest.raises(ValueError):
        policy.render_action("removable_disk_image", {"disk_index": 2, "output_root": "C:\\Windows\\Temp", "chunk_mb": 4})


def test_removable_disk_probe_is_read_only_and_removable_only() -> None:
    rendered = policy.render_action("removable_disk_probe", {"disk_index": 2})

    assert rendered.action == "removable_disk_probe"
    assert "Win32_DiskDrive" in rendered.command
    assert "refusing to probe non-removable disk" in rendered.command
    assert "[System.IO.FileAccess]::Read" in rendered.command
    assert "\\\\.\\PhysicalDrive$diskIndex" in rendered.command


def test_removable_disk_reinitialize_is_destructive_but_removable_only() -> None:
    rendered = policy.render_action(
        "removable_disk_reinitialize",
        {"disk_index": 2, "filesystem": "exfat", "label": "SDCARD", "drive_letter": "E"},
        timeout=99999,
    )

    assert rendered.action == "removable_disk_reinitialize"
    assert "refusing to reinitialize non-removable disk" in rendered.command
    assert "select disk 2" in rendered.command
    assert "clean" in rendered.command
    assert "format fs=exfat quick" in rendered.command
    assert "assign letter=E" in rendered.command
    assert rendered.timeout == 1800


def test_removable_disk_reinitialize_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        policy.render_action(
            "removable_disk_reinitialize",
            {"disk_index": 2, "filesystem": "refs", "label": "SDCARD", "drive_letter": "E"},
        )
    with pytest.raises(ValueError):
        policy.render_action(
            "removable_disk_reinitialize",
            {"disk_index": 2, "filesystem": "exfat", "label": "bad&label", "drive_letter": "E"},
        )
    with pytest.raises(ValueError):
        policy.render_action(
            "removable_disk_reinitialize",
            {"disk_index": 2, "filesystem": "exfat", "label": "SDCARD", "drive_letter": "C"},
        )


def test_removable_volume_format_is_limited_to_removable_drive_letter() -> None:
    rendered = policy.render_action(
        "removable_volume_format",
        {"drive_letter": "E", "filesystem": "exfat", "label": "SDCARD"},
    )

    assert rendered.action == "removable_volume_format"
    assert "DriveType -ne 2" in rendered.command
    assert "format $drive /FS:EXFAT /Q /Y /V:SDCARD" in rendered.command


def test_removable_volume_format_rejects_system_drive() -> None:
    with pytest.raises(ValueError):
        policy.render_action(
            "removable_volume_format",
            {"drive_letter": "C", "filesystem": "exfat", "label": "SDCARD"},
        )


def test_unknown_action_is_denied() -> None:
    with pytest.raises(ValueError):
        policy.render_action("powershell", {"command": "whoami"})
