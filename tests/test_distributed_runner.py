from pathlib import Path
import os
import re
import subprocess
import sys
import types

import pytest

from twindash import settings


@pytest.fixture
def runner():
    root = Path(os.environ.get("TWINDASH_GENERATOR_REPO", settings.repo_root()))
    source = root / "deploy_ric5g.sh"
    if not source.is_file():
        pytest.skip("Set TWINDASH_GENERATOR_REPO to the traffic generator repository")
    return source.read_text()


def test_uplink_staging_uses_live_dn_without_changing_traffic(runner, tmp_path):
    match = re.search(r"(stage_uplink\(\) \{\n.*?\nPYEOF\n\})", runner, re.S)
    assert match is not None
    source = tmp_path / "ue1_ul_tx.mgn"
    destination = tmp_path / "staged.mgn"
    original = (
        "0.5 ON 1 UDP SRC 1025 DST 192.168.70.163/6000 PERIODIC [10 100]\n"
        "1.0 OFF 1\n"
        "2 ON 2 UDP SRC 1026 DST 192.168.70.163/6001 PERIODIC [20 200]\n"
        "3 OFF 2\n")
    source.write_text(original)
    script = match[1] + '\nDN_IP=192.168.72.135\nstage_uplink "$1" "$2"\n'
    subprocess.run(["bash", "-c", script, "stage", str(source), str(destination)], check=True)
    assert source.read_text() == original
    assert destination.read_text() == original.replace("192.168.70.163", "192.168.72.135")


@pytest.mark.parametrize("now,duration,expected", [(539, 900, 461), (1001, 900, 0), (100.2, 1, .8)])
def test_observation_wait_covers_idle_tail(runner, monkeypatch, now, duration, expected):
    match = re.search(r'python3 - "\$CHANNEL_START" "\$DURATION" <<\x27PYEOF\x27\n(.*?)\nPYEOF', runner, re.S)
    assert match is not None
    current = [50.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        current[0] += seconds

    timer = types.SimpleNamespace(time=lambda: now, monotonic=lambda: current[0], sleep=sleep)
    monkeypatch.setitem(sys.modules, "time", timer)
    monkeypatch.setattr(sys, "argv", ["wait", "100", str(duration)])
    exec(compile(match[1], "observation_window", "exec"), {})
    assert sum(sleeps) == pytest.approx(expected)
    assert all(0 < value <= 30 for value in sleeps)


def test_checked_core_address_is_required(runner):
    match = re.search(r'(DN_IP=\$\(printf.*?\n\[ -n "\$DN_IP" \].*?\n)', runner)
    assert match is not None
    script = 'CORE_CHECK="$1"\n' + match[1] + '\nprintf "%s" "$DN_IP"\n'
    valid = subprocess.run(["bash", "-c", script, "check", "OK: external DN is 192.168.72.135"],
                           capture_output=True, text=True)
    assert valid.returncode == 0 and valid.stdout == "192.168.72.135"
    missing = subprocess.run(["bash", "-c", script, "check", "OK: MGEN executes"],
                             capture_output=True, text=True)
    assert missing.returncode != 0
