# Disclaimer

**Quarto Dashboard Delivery System**  
Copyright © 2026 **Chrissy h. Roberts**

This software is provided **as is** and **without guarantee or warranty of any kind**.

The author does not guarantee that the software is error-free, uninterrupted, secure, suitable for a particular purpose, compliant with any specific institutional or regulatory requirement, or capable of preventing data loss, disclosure, corruption, misdelivery or analytical error.

Users and deploying organisations are responsible for validating the software and every dashboard configured to use it before production operation. This includes, as applicable:

- verifying source-data acquisition and transformation;
- verifying analytical and scientific correctness;
- verifying target routing and recipient permissions;
- verifying Microsoft OneDrive/SharePoint or other remote configuration;
- protecting API tokens, credentials and configuration files;
- implementing appropriate host, account and filesystem security;
- implementing independent backup, retention and disaster-recovery arrangements;
- checking logs and scheduled-run failures;
- complying with GDPR and other applicable privacy/data-protection law;
- obtaining and following relevant research governance, ethics, sponsor, institutional and information-security approvals;
- deciding whether timestamped delivery archives are appropriate for the applicable data-retention policy;
- determining whether the software is suitable for clinical, research, operational or regulated use.

The controller contains safeguards intended to reduce common operational risks, including restricted cleanup paths, separation of secrets from dashboard YAML, sanitisation of common dashboard publication output, transactional archiving of previous target deliveries, verification before replacement, and retention of working products after incomplete delivery. These safeguards reduce risk; they do not eliminate it.

The timestamped `Archive/` folders created by this system are delivery history. They are **not a substitute for an institutional backup system**.

The software must not be treated as a substitute for human review of access permissions, source data, analytical output or publication content.

Use of the software is at the user's and deploying organisation's own risk.

The software licence itself is contained in [LICENSE](LICENSE) and is the MIT License.
