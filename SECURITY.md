# Security

## Supported deployment model

TwinDash is intended for a trusted, single-user operator workstation connected
to infrastructure that the operator is authorized to control. The application
can connect to remote nodes and start experiments, so do not expose the
Streamlit service to an untrusted network.

Before any public or multi-user deployment, add authentication, authorization,
TLS, audit logging, secret management, and an explicit policy for remote-command
access. Keep POWDER hostnames, usernames, credentials, reservation details, and
generated `testbed_config.yaml` files out of commits.

## Reporting a vulnerability

Please report security issues privately through the repository's GitHub
Security Advisory interface. Include the affected revision, reproduction steps,
impact, and any suggested mitigation. Do not include credentials, reservation
details, or other secrets in a public issue.
