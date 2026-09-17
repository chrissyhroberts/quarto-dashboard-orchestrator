# Maintainer notes

## Design boundary

The repository root is the reusable controller. Each direct child of `dashboards/` is an independent Quarto project with its own `dashboard.yml`. Keep study-specific logic in those project folders.

## Release checks

1. Run unit tests.
2. Run `./dashboardctl validate`.
3. Test both placeholder dashboards on a machine with Quarto installed.
4. Verify an XLSX-routed product is a real workbook, not a renamed CSV.
5. Verify target routing does not leak one target's files to another.
6. Check `.gitignore` before committing.
7. Inspect `git status` for secrets, source data and generated outputs.

## Deployment safety

If syncing this repository to another machine with `rsync --delete`, dry-run first and explicitly exclude machine-owned runtime files such as `config/secrets.env`, `config/rclone.conf`, `config/runtime.env`, `logs/` and generated delivery folders. Do not assume a source-tree deletion is safe for a runtime directory.
