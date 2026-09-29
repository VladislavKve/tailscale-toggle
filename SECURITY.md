# Security policy

## Reporting a vulnerability

Please do not disclose authentication, privilege-boundary, or secret-handling
issues in a public issue. Use GitHub's private vulnerability reporting:

https://github.com/VladislavKve/tailscale-toggle/security/advisories/new

Include the affected version, reproduction steps, expected impact, and whether
the report involved a real or synthetic Tailscale account. Never attach active
auth keys, login URLs, or unredacted tailnet information.

## Scope

The project wraps the locally installed Tailscale CLI. It does not implement a
coordination server, identity provider, or VPN protocol. Reports about the
official Tailscale service or client should be sent to Tailscale through its
own security process.
