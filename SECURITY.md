# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or include a
working exploit, credential, private profile data, database, or log in a public
pull request.

Use GitHub's **Security** tab to submit a private vulnerability report. Include
the affected route or component, impact, reproduction steps, and any suggested
mitigation. You should receive an acknowledgement within seven days.

If private vulnerability reporting is unavailable, open a minimal public issue
asking the maintainer to enable a private contact channel. Do not disclose the
technical details there.

## Supported versions

Security fixes are made on the latest `main` revision. There are currently no
separately supported release branches.

## Deployment notes

- Use your own Steam API key and never expose `.env` through the document root.
- Keep the public API behind a TLS-terminating reverse proxy.
- The Compose defaults bind both public and admin ports to loopback.
- Do not publish the admin service directly. Prefer an SSH or private-network
  tunnel and retain its IP allow list, password, and `ADMIN_TOKEN` controls.
- Configure forwarded-client-IP trust only for proxies you operate. Trusting
  arbitrary forwarding headers defeats address-based rate limits.
- Back up and protect the persistent data volume. It contains user-submitted
  content and salted identifiers even though it contains no raw client IPs.
- Review the frontend privacy policy and retention settings before accepting
  public traffic.
