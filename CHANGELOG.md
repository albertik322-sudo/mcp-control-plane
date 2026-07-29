# Changelog

All notable changes to this repository are documented here.

## 0.2.0 - 2026-07-29

### Changed

- Prepared the repository for its first public release under the MIT License.
- Replaced personal domains, LAN topology, usernames, and local paths with
  environment-driven or documentation-only examples.
- Kept game integrations and unrelated operational projects out of scope.

## 0.1.1 - 2026-07-29

### Fixed

- Added a sanitized Codex MCP registration template.
- Made the control-plane audit deterministic on clean GitHub Actions runners.
- Updated Pillow and Starlette to security-fixed releases.

## 0.1.0 - 2026-07-29

### Added

- Clean public GitHub publication with a curated, topology-neutral source snapshot.
- MCP 2.0 stateless runtime across 15 server entrypoints.
- JSON Schema 2020-12 structured output contracts.
- Opt-in OpenTelemetry export with metadata-only middleware.
- MCP Apps support with text fallback.
- Durable internal Tasks pilot with truthful capability detection.
- Stateless conformance, offline tests, and control-plane security audit.
- Repository-owned Agent Skill workflow through `gh skill`.

### Security

- Excluded local secrets, captures, firmware, archives, snapshots, runtime
  databases, generated artifacts, and vendored tools.
- Published from a clean history after redacted secret scanning.
