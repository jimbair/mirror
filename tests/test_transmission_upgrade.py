#!/usr/bin/env python3
"""Exercise upgrade service recovery without root or host commands."""

import subprocess
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / 'transmission-upgrade.sh'


class TestUpgradeServiceRecovery(unittest.TestCase):
    def _run(self, *, running=True, stop_result=0, install_result=0,
             start_result=0, interrupt=False):
        # Shell functions replace every host operation used by install_build.
        # The invocation log and state model let us check ordering and the
        # final service state without installing anything or using systemd.
        body = '''source "$1"
active=$2
stop_result=$3
install_result=$4
start_result=$5
interrupt=$6
log() { printf 'LOG:%s\\n' "$*"; }
sleep() { :; }
systemctl() {
    printf 'SYSTEMCTL:%s\\n' "$*"
    case "$1" in
        is-active) [[ "$active" == true ]] ;;
        stop)
            [[ "$stop_result" == 0 ]] || return "$stop_result"
            active=false ;;
        start)
            [[ "$start_result" == 0 ]] || return "$start_result"
            active=true ;;
        *) return 99 ;;
    esac
}
cmake() {
    printf 'INSTALL:%s\\n' "$*"
    if [[ "$interrupt" == true ]]; then kill -TERM "$$"; fi
    return "$install_result"
}
install_build
printf 'FINAL_ACTIVE:%s\\n' "$active"
'''
        return subprocess.run(
            ['bash', '-c', body, 'transmission-upgrade-test', str(SCRIPT),
             str(running).lower(), str(stop_result), str(install_result),
             str(start_result), str(interrupt).lower()],
            capture_output=True, text=True, timeout=5)

    def test_successful_upgrade_restarts_an_active_service(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [line for line in result.stdout.splitlines()
                 if line.startswith(('SYSTEMCTL:', 'INSTALL:'))]
        self.assertEqual(calls, [
            'SYSTEMCTL:is-active --quiet transmission-daemon',
            'SYSTEMCTL:stop transmission-daemon',
            'INSTALL:--install build',
            'SYSTEMCTL:start transmission-daemon',
            'SYSTEMCTL:is-active --quiet transmission-daemon',
        ])
        self.assertIn('FINAL_ACTIVE:true', result.stdout)

    def test_successful_upgrade_keeps_an_inactive_service_stopped(self):
        result = self._run(running=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('INSTALL:--install build', result.stdout)
        self.assertNotIn('SYSTEMCTL:stop', result.stdout)
        self.assertNotIn('SYSTEMCTL:start', result.stdout)
        self.assertIn('FINAL_ACTIVE:false', result.stdout)

    def test_failed_stop_aborts_before_installation(self):
        result = self._run(stop_result=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('INSTALL:', result.stdout)
        self.assertIn('install aborted', result.stderr)

    def test_failed_install_attempts_service_recovery_and_still_fails(self):
        result = self._run(install_result=9)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Install stage failed', result.stderr)
        self.assertIn('Restoring transmission-daemon', result.stdout)
        self.assertIn('SYSTEMCTL:start transmission-daemon', result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith(
            'SYSTEMCTL:is-active --quiet transmission-daemon'))

    def test_failed_install_does_not_start_an_originally_inactive_service(self):
        result = self._run(running=False, install_result=9)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('SYSTEMCTL:start', result.stdout)

    def test_failed_recovery_is_reported(self):
        result = self._run(install_result=9, start_result=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Install stage failed', result.stderr)
        self.assertIn('Could not restore transmission-daemon', result.stderr)

    def test_failed_restart_is_reported(self):
        result = self._run(start_result=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('transmission-daemon failed to start', result.stderr)
        self.assertIn('Could not restore transmission-daemon', result.stderr)

    def test_interrupted_install_restores_service_and_preserves_failure(self):
        result = self._run(interrupt=True)
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertIn('SYSTEMCTL:start transmission-daemon', result.stdout)
        self.assertIn('SYSTEMCTL:is-active --quiet transmission-daemon', result.stdout)


if __name__ == '__main__':
    unittest.main()
