# Contributing

Issues and focused pull requests are welcome.

## Before opening a pull request

1. Keep the application user-scoped and avoid changing Tailscale state during
   tests or previews.
2. Never commit real tailnet names, device names, Tailscale IPs, auth keys,
   login URLs, or profile identifiers.
3. Preserve the security properties of auth keys: masked input, no persistence,
   no raw key in argv, mode-`0600` temporary files, redaction, and cleanup.
4. Use the official browser/identity-provider flow for password-based sign-in.
5. Keep GTK 4 in the main process and GTK 3 AppIndicator in the tray process.

Run the checks before submitting:

```bash
/usr/bin/python3 -B -m py_compile tailscale_core.py tailscale_toggle.py tray_agent.py
/usr/bin/python3 -B -m unittest discover -s tests -v
./run.sh --preview connected --no-tray --quit-after 2
```

Do not run real `tailscale login`, `tailscale up`, `tailscale logout`, or
`tailscale switch` as part of automated tests.
