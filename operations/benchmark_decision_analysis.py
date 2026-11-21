"""Reproduce the synthetic latency comparison; does not contact any model."""
import os
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.test_settings'
import django
django.setup()

from delivery.decision_team_engine import _call, _parallel_analyses, make_state
from delivery.test_decision_team import connections


def simulated_provider(*args, **kwargs):
    time.sleep(0.2)
    return '{}'


def main():
    team = connections()
    parts = make_state(team, [])['participants']
    noop = lambda *_: None
    serial, parallel = [], []
    with patch('delivery.decision_team_engine.generate_reply', side_effect=simulated_provider):
        for _ in range(3):
            started = time.monotonic()
            for part in parts:
                _call('analysis_' + part['id'], part['label'], team[part['role']], {}, '分析', noop, noop)
            serial.append(time.monotonic() - started)
            started = time.monotonic()
            _parallel_analyses(parts, team, {}, '分析', noop, noop, lambda: 3)
            parallel.append(time.monotonic() - started)
    a, b = statistics.median(serial), statistics.median(parallel)
    print(f'3 synthetic providers at 200 ms each, 3 runs: serial {a:.3f}s, parallel {b:.3f}s; speedup {a / b:.2f}x')


if __name__ == '__main__':
    main()
