# Production readiness

Run the offline release gate before every merge or tagged release:

```bash
source .venv/bin/activate
bash bin/production-readiness.sh
```

To require a real Plotly-to-PDF conversion as well:

```bash
TWINDASH_CHECK_PDF=1 bash bin/production-readiness.sh
```

Pass a generated run name to include its non-mutating local preflight:

```bash
bash bin/production-readiness.sh run_example_20260723_120000
```

The gate checks Python compilation, the complete automated test suite, all four
Streamlit pages in the headless AppTest runtime, and the CLI entry point. CI
runs it on Python 3.10 and 3.12.

## Live POWDER acceptance

The automated gate deliberately does not start traffic or mutate a reservation.
Before calling a release production-ready, complete one acceptance execution:

1. Save valid core and cell hosts on the Testbed page.
2. Confirm every expected UE is attached and the RIC reports the expected E2
   associations with zero stale E42 associations.
3. Run a short scenario with a verified channel schedule.
4. Require all expected xApp subscriptions and delete responses, zero xApp
   errors, clean shutdown, complete UE coverage, a valid dual radio clock, and
   verified channel labels.
5. Archive the execution. Export it once and retain the generated checksums.
6. Run the same acceptance test once more at the maximum supported topology.

## Deployment model

This dashboard is suitable for a trusted single-user operator workstation.
Do not bind Streamlit to a public interface without adding authentication,
authorization, TLS, audit logging, and secret management. A per-run file lock
prevents two local browser sessions or CLI processes from deploying the same
run concurrently.
