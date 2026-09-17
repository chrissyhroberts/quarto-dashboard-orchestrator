# Security

## Secrets

Never commit `config/secrets.env`, `config/rclone.conf`, API tokens, passwords, source data or participant-level outputs. The supplied `.gitignore` excludes the normal runtime locations.

## Data protection

This repository provides delivery mechanics, not an access-control policy. Before using real data, define who may receive each product and validate the configured `targets` and `products` routes.

## Reporting a security issue

For a public fork, use the repository owner's private security-reporting channel rather than opening an issue containing credentials or sensitive data.
