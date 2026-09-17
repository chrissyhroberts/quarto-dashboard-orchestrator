# Changelog

## 1.0.1 — 2026-09-17

Documentation clarification release.

- Added top-level compatibility matrix
- Explicitly separated controller responsibilities from Quarto project responsibilities
- Clarified that XLSForm semantics are not part of the controller
- Documented built-in source connectors and their received formats
- Documented publication formats, routed-product behaviour and delivery targets
- Added `docs/ARCHITECTURE.md`

## 1.0.0 — 2026-09-17

Initial generic starter release.

- Multi-dashboard Quarto discovery and orchestration
- KoboToolbox, ODK Central, REDCap, project-managed or no-source acquisition
- Local and SharePoint/rclone delivery
- Audience/target routing of operational products
- Optional CSV → XLSX recipient delivery
- Current/Archive transactions for routed action products
- SHA-256 verification and audit logging
- Post-run cleanup controls
- Two self-contained placeholder dashboards
- GitHub Actions unit-test workflow

Study-specific dashboards and deployment-specific submission-archive publishers are deliberately not part of this generic starter.
