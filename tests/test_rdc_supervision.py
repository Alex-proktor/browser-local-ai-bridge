import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PS = shutil.which('powershell.exe')
pytestmark = pytest.mark.skipif(not PS, reason='Windows PowerShell runtime required')
SCRIPTS = Path(__file__).resolve().parents[1] / 'tools' / 'windows'


def ps(code):
    helper = str(SCRIPTS / 'rdc-common.ps1').replace("'", "''")
    result = subprocess.run(
        [PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command',
         "$ErrorActionPreference='Stop'; . '" + helper + "'; " + code],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize('now,allowed', [
    ('2026-10-07T12:01:59Z', False),
    ('2026-10-07T12:02:00Z', True),
    ('2026-10-07T15:02:00+03:00', True),
])
def test_backoff_obeys_elapsed_time_across_offsets(now, allowed):
    output = ps(
        "$state=[pscustomobject]@{Count=1;LastAttemptUtc='2026-10-07T12:00:00Z'}; "
        f"Get-RdcRetryPlan $state ([datetimeoffset]'{now}') | ConvertTo-Json -Compress"
    )
    assert json.loads(output)['Allowed'] is allowed


def test_clock_rollback_cannot_strand_device():
    output = ps(
        "$state=[pscustomobject]@{Count=99;LastAttemptUtc='2026-10-08T12:00:00Z'}; "
        "Get-RdcRetryPlan $state ([datetimeoffset]'2026-10-07T12:00:00Z') | ConvertTo-Json -Compress"
    )
    assert json.loads(output)['Allowed'] is True
    assert json.loads(output)['Count'] == 0


def test_repeated_failures_have_thirty_minute_backoff():
    output = ps(
        "$state=[pscustomobject]@{Count=99;LastAttemptUtc='2026-10-07T12:00:00Z'}; "
        "Get-RdcRetryPlan $state ([datetimeoffset]'2026-10-07T12:29:00Z') | ConvertTo-Json -Compress"
    )
    assert json.loads(output)['Allowed'] is False
    assert json.loads(output)['DelayMinutes'] == 30


def test_intact_entry_does_not_hide_missing_dependency(tmp_path):
    (tmp_path / 'dist').mkdir()
    (tmp_path / 'dist' / 'index.js').write_bytes(b'export {};')
    (tmp_path / 'package.json').write_text(json.dumps({
        'version': '0.2.51', 'dependencies': {'missing-sdk': '^1.0.0'},
    }))
    digest = hashlib.sha256(b'export {};').hexdigest().upper()
    path = str(tmp_path).replace("'", "''")
    assert ps(f"$RdcEntryHash='{digest}'; Get-RdcPackageProblem '{path}'") == 'dependency missing: missing-sdk'
    dependency = tmp_path / 'node_modules' / 'missing-sdk'
    dependency.mkdir(parents=True)
    (dependency / 'package.json').write_text('{}')
    assert ps(f"$RdcEntryHash='{digest}'; Get-RdcPackageProblem '{path}'") == ''

