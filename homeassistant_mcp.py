from __future__ import annotations

import os
import json as jsonlib
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import websocket
from dotenv import load_dotenv
from mcp_runtime import configure_stdio, create_mcp_server


configure_stdio()
load_dotenv(encoding="utf-8-sig")
mcp = create_mcp_server("home-assistant")


def _base_url() -> str:
    return os.getenv("HOMEASSISTANT_URL", "http://192.168.1.20:8123").rstrip("/")


def _headers() -> dict[str, str]:
    token = os.getenv("HOMEASSISTANT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("HOMEASSISTANT_TOKEN is not set. Create a long-lived access token in Home Assistant profile.")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _timeout() -> float:
    return float(os.getenv("HOMEASSISTANT_TIMEOUT", "15"))


def _request(method: str, path: str, json: Any | None = None, params: dict[str, Any] | None = None) -> Any:
    started = time.time()
    url = f"{_base_url()}{path}"
    with httpx.Client(timeout=_timeout()) as client:
        response = client.request(method, url, headers=_headers(), json=json, params=params)
    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text
    if response.status_code >= 400:
        raise RuntimeError({"status_code": response.status_code, "response": payload})
    if isinstance(payload, dict):
        payload.setdefault("_duration_seconds", round(time.time() - started, 3))
    return payload


def _ws_url() -> str:
    base = _base_url()
    if base.startswith("https://"):
        return "wss://" + base.removeprefix("https://") + "/api/websocket"
    if base.startswith("http://"):
        return "ws://" + base.removeprefix("http://") + "/api/websocket"
    raise RuntimeError(f"unsupported HOMEASSISTANT_URL: {base}")


def _ws_request(message_type: str, **payload: Any) -> Any:
    token = os.getenv("HOMEASSISTANT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("HOMEASSISTANT_TOKEN is not set. Create a long-lived access token in Home Assistant profile.")
    timeout = _timeout()
    ws = websocket.create_connection(_ws_url(), timeout=timeout)
    try:
        hello = ws.recv()
        if '"auth_required"' not in hello:
            raise RuntimeError({"reason": "unexpected websocket greeting", "response": hello})
        ws.send(jsonlib.dumps({"type": "auth", "access_token": token}))
        auth = ws.recv()
        if '"auth_ok"' not in auth:
            raise RuntimeError({"reason": "websocket auth failed", "response": auth})
        ws.send(jsonlib.dumps({"id": 1, "type": message_type, **payload}))
        while True:
            response = ws.recv()
            data = jsonlib.loads(response)
            if data.get("id") == 1:
                if not data.get("success", False):
                    raise RuntimeError(data)
                return data.get("result")
    finally:
        ws.close()


@mcp.tool()
def health() -> dict[str, Any]:
    """Check Home Assistant API reachability and token validity."""
    try:
        payload = _request("GET", "/api/")
        return {"ok": True, "url": _base_url(), "response": payload}
    except Exception as exc:
        return {"ok": False, "url": _base_url(), "error": str(exc)}


@mcp.tool()
def config() -> dict[str, Any]:
    """Return Home Assistant core configuration."""
    return _request("GET", "/api/config")


@mcp.tool()
def states(entity_id: str | None = None) -> Any:
    """Return all states, or a single entity state when entity_id is provided."""
    if entity_id:
        return _request("GET", f"/api/states/{entity_id}")
    rows = _request("GET", "/api/states")
    return {"count": len(rows), "states": rows}


@mcp.tool()
def search_states(query: str, domain: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Search entities by entity_id, state, friendly_name, device_class, or unit."""
    query_l = query.lower()
    rows = _request("GET", "/api/states")
    matches: list[dict[str, Any]] = []
    for row in rows:
        entity_id = row.get("entity_id", "")
        if domain and not entity_id.startswith(f"{domain}."):
            continue
        attrs = row.get("attributes", {}) or {}
        haystack = " ".join(
            str(value)
            for value in [
                entity_id,
                row.get("state", ""),
                attrs.get("friendly_name", ""),
                attrs.get("device_class", ""),
                attrs.get("unit_of_measurement", ""),
            ]
        ).lower()
        if query_l in haystack:
            matches.append(row)
            if len(matches) >= max(1, int(limit)):
                break
    return {"count": len(matches), "matches": matches}


@mcp.tool()
def services() -> Any:
    """Return available Home Assistant service domains and service schemas."""
    rows = _request("GET", "/api/services")
    return {"count": len(rows), "services": rows}


@mcp.tool()
def ws_command(message_type: str, payload: dict[str, Any] | None = None) -> Any:
    """Run a raw Home Assistant WebSocket command."""
    return _ws_request(message_type, **(payload or {}))


@mcp.tool()
def areas() -> dict[str, Any]:
    """Return Home Assistant area registry."""
    rows = _ws_request("config/area_registry/list")
    return {"count": len(rows), "areas": rows}


@mcp.tool()
def devices() -> dict[str, Any]:
    """Return Home Assistant device registry."""
    rows = _ws_request("config/device_registry/list")
    return {"count": len(rows), "devices": rows}


@mcp.tool()
def entities() -> dict[str, Any]:
    """Return Home Assistant entity registry."""
    rows = _ws_request("config/entity_registry/list")
    return {"count": len(rows), "entities": rows}


@mcp.tool()
def yandex_entities() -> dict[str, Any]:
    """Return entities and devices that belong to Yandex integrations."""
    entity_rows = _ws_request("config/entity_registry/list")
    device_rows = _ws_request("config/device_registry/list")
    y_entities = [
        row
        for row in entity_rows
        if str(row.get("platform", "")).startswith("yandex") or "yandex" in str(row.get("entity_id", "")).lower()
    ]
    y_device_ids = {row.get("device_id") for row in y_entities if row.get("device_id")}
    y_devices = [
        row
        for row in device_rows
        if row.get("id") in y_device_ids or str(row.get("manufacturer", "")).lower().startswith("yandex")
    ]
    return {
        "entity_count": len(y_entities),
        "device_count": len(y_devices),
        "entities": y_entities,
        "devices": y_devices,
    }


@mcp.tool()
def call_service(domain: str, service: str, service_data: dict[str, Any] | None = None) -> Any:
    """Call a Home Assistant service, for example domain='light', service='turn_on'."""
    rows = _request("POST", f"/api/services/{domain}/{service}", json=service_data or {})
    if isinstance(rows, list):
        return {"count": len(rows), "states": rows}
    return rows


@mcp.tool()
def turn_on(entity_id: str) -> Any:
    """Turn on an entity through homeassistant.turn_on."""
    return call_service("homeassistant", "turn_on", {"entity_id": entity_id})


@mcp.tool()
def turn_off(entity_id: str) -> Any:
    """Turn off an entity through homeassistant.turn_off."""
    return call_service("homeassistant", "turn_off", {"entity_id": entity_id})


@mcp.tool()
def toggle(entity_id: str) -> Any:
    """Toggle an entity through homeassistant.toggle."""
    return call_service("homeassistant", "toggle", {"entity_id": entity_id})


@mcp.tool()
def light_set(entity_id: str, brightness_pct: int | None = None, rgb_color: list[int] | None = None) -> Any:
    """Turn on a light and optionally set brightness_pct and rgb_color."""
    data: dict[str, Any] = {"entity_id": entity_id}
    if brightness_pct is not None:
        data["brightness_pct"] = max(0, min(100, int(brightness_pct)))
    if rgb_color is not None:
        if len(rgb_color) != 3:
            raise ValueError("rgb_color must contain exactly 3 integers")
        data["rgb_color"] = [max(0, min(255, int(value))) for value in rgb_color]
    return call_service("light", "turn_on", data)


@mcp.tool()
def media_player(entity_id: str, action: str, value: float | None = None) -> Any:
    """Control a media_player: play, pause, stop, next, previous, mute, unmute, volume_set."""
    service_map = {
        "play": "media_play",
        "pause": "media_pause",
        "stop": "media_stop",
        "next": "media_next_track",
        "previous": "media_previous_track",
        "mute": "volume_mute",
        "unmute": "volume_mute",
        "volume_set": "volume_set",
    }
    if action not in service_map:
        raise ValueError(f"action must be one of: {', '.join(sorted(service_map))}")
    data: dict[str, Any] = {"entity_id": entity_id}
    if action == "volume_set":
        if value is None:
            raise ValueError("value is required for volume_set and must be between 0.0 and 1.0")
        data["volume_level"] = max(0.0, min(1.0, float(value)))
    if action in {"mute", "unmute"}:
        data["is_volume_muted"] = action == "mute"
    return call_service("media_player", service_map[action], data)


@mcp.tool()
def yandex_station_command(entity_id: str, command: str, data: dict[str, Any] | None = None) -> Any:
    """Send a native command to a Yandex.Station media_player entity."""
    payload = {"entity_id": entity_id, "command": command}
    payload.update(data or {})
    return call_service("yandex_station", "send_command", payload)


@mcp.tool()
def yandex_station_say(entity_id: str, text: str) -> Any:
    """Make a Yandex.Station say text through the sendText command."""
    return yandex_station_command(entity_id, "sendText", {"text": text})


@mcp.tool()
def yandex_station_set_volume(entity_id: str, volume: float) -> Any:
    """Set Yandex.Station volume from 0.0 to 1.0."""
    return yandex_station_command(entity_id, "setVolume", {"volume": max(0.0, min(1.0, float(volume)))})


def _yandex_media_players() -> list[dict[str, Any]]:
    rows = _request("GET", "/api/states")
    return [
        row
        for row in rows
        if str(row.get("entity_id", "")).startswith("media_player.yandex_")
        or str((row.get("attributes") or {}).get("friendly_name", "")).lower().startswith(("яндекс", "станция"))
    ]


def _find_yandex_media_player(name: str) -> dict[str, Any]:
    query = name.lower()
    matches = []
    for row in _yandex_media_players():
        entity_id = str(row.get("entity_id", ""))
        friendly = str((row.get("attributes") or {}).get("friendly_name", ""))
        if query in entity_id.lower() or query in friendly.lower():
            matches.append(row)
    if not matches:
        raise RuntimeError({"reason": "no matching Yandex media_player", "query": name})
    available = [row for row in matches if row.get("state") != "unavailable"]
    return available[0] if available else matches[0]


@mcp.tool()
def yandex_media_players() -> dict[str, Any]:
    """Return Yandex media_player entities with friendly names and availability."""
    rows = _yandex_media_players()
    return {
        "count": len(rows),
        "players": [
            {
                "entity_id": row.get("entity_id"),
                "state": row.get("state"),
                "friendly_name": (row.get("attributes") or {}).get("friendly_name"),
                "volume_level": (row.get("attributes") or {}).get("volume_level"),
                "alice_state": (row.get("attributes") or {}).get("alice_state"),
            }
            for row in rows
        ],
    }


@mcp.tool()
def yandex_station_say_by_name(name: str, text: str) -> Any:
    """Make a Yandex media_player say text by friendly name or entity_id substring."""
    row = _find_yandex_media_player(name)
    return yandex_station_say(str(row["entity_id"]), text)


@mcp.tool()
def yandex_station_set_volume_by_name(name: str, volume: float) -> Any:
    """Set Yandex media_player volume by friendly name or entity_id substring."""
    row = _find_yandex_media_player(name)
    return yandex_station_set_volume(str(row["entity_id"]), volume)


@mcp.tool()
def history(entity_id: str, start_time: str | None = None, minimal_response: bool = True) -> Any:
    """Return entity history from ISO start_time, or from now when omitted."""
    if start_time is None:
        start_time = datetime.now(timezone.utc).isoformat()
    params = {
        "filter_entity_id": entity_id,
        "minimal_response": "1" if minimal_response else "0",
    }
    rows = _request("GET", f"/api/history/period/{start_time}", params=params)
    return {"count": len(rows), "history": rows}


@mcp.tool()
def raw_get(path: str) -> Any:
    """Run a raw authenticated GET against /api. Path must start with /api/."""
    if not path.startswith("/api/"):
        raise ValueError("path must start with /api/")
    return _request("GET", path)


if __name__ == "__main__":
    mcp.run()
