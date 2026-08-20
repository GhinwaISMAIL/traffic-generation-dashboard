#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

PYTHON=${PYTHON:-python3}
export PYTHONPATH="$ROOT"
export PYTHONPYCACHEPREFIX=${PYTHONPYCACHEPREFIX:-"${TMPDIR:-/tmp}/twindash-pycache"}

echo "== compile =="
"$PYTHON" -m compileall -q twindash dashboard tests

echo "== automated tests =="
"$PYTHON" -m pytest -q

echo "== CLI entry point =="
"$PYTHON" -m twindash.cli --help >/dev/null

if [[ ${TWINDASH_CHECK_PDF:-0} == 1 ]]; then
    echo "== live Plotly PDF export =="
    "$PYTHON" - <<'PY'
import plotly.graph_objects as go

document = go.Figure(
    go.Scatter(x=[0, 1], y=[0, 1])
).to_image(format="pdf")
if not document.startswith(b"%PDF"):
    raise SystemExit("Plotly did not return a PDF document")
print(f"PDF export passed ({len(document)} bytes)")
PY
else
    echo "== live Plotly PDF export skipped (set TWINDASH_CHECK_PDF=1 to require it) =="
fi

if [[ $# -gt 0 ]]; then
    echo "== local run preflight: $1 =="
    "$PYTHON" -m twindash.cli preflight "$1"
fi

echo "RELEASE VALIDATION GATE PASSED"
