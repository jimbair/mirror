#!/usr/bin/env python3
"""Exercise upgrade preparation, verification, and service recovery safely."""

import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / 'transmission-upgrade.sh'


class TestUpgradePreparation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='transmission upgrade test ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sources = self.root / 'sources'
        self.sources.mkdir()
        self.build = self.sources / 'transmission-4.1.4'
        self.archive = self.root / 'release.tar'
        with tarfile.open(self.archive, 'w') as archive:
            payload = b'# disposable source fixture\n'
            info = tarfile.TarInfo('transmission-4.1.4/CMakeLists.txt')
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    def _run(self, stubs=''):
        body = '''source "$1"
SRC_DIR=$2
TARBALL_PATH=$3
TARBALL=release.tar
BUILD_DIR=$4
''' + stubs + '''
prepare_build
printf 'BUILD_REACHED:%s\\n' "$PWD"
'''
        return subprocess.run(
            ['bash', '-c', body, 'upgrade-preparation-test', str(SCRIPT),
             str(self.sources), str(self.archive), str(self.build)],
            cwd=self.root, capture_output=True, text=True, timeout=5)

    def test_success_extracts_and_enters_the_expected_source_directory(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('BUILD_REACHED:' + str(self.build), result.stdout)
        self.assertEqual((self.build / 'CMakeLists.txt').read_bytes(),
                         b'# disposable source fixture\n')

    def test_success_removes_stale_source_before_extraction(self):
        self.build.mkdir()
        (self.build / 'stale.txt').write_text('old build')
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.build / 'stale.txt').exists())
        self.assertTrue((self.build / 'CMakeLists.txt').is_file())

    def test_failed_stale_cleanup_stops_before_extraction(self):
        self.build.mkdir()
        stale = self.build / 'stale.txt'
        stale.write_text('old build')
        result = self._run('''rm() { return 7; }
tar() { printf 'TAR_REACHED\\n'; }
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not remove stale build', result.stderr)
        self.assertNotIn('TAR_REACHED', result.stdout)
        self.assertNotIn('BUILD_REACHED', result.stdout)
        self.assertEqual(stale.read_text(), 'old build')

    def test_failed_extraction_with_partial_directory_stops_before_build(self):
        result = self._run('''tar() {
    mkdir -p "$BUILD_DIR"
    printf partial > "$BUILD_DIR/partial.txt"
    return 17
}
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Tarball extraction failed', result.stderr)
        self.assertNotIn('BUILD_REACHED', result.stdout)
        self.assertEqual((self.build / 'partial.txt').read_text(), 'partial')
        self.assertTrue(self.archive.is_file())

    def test_missing_expected_directory_stops_before_build(self):
        result = self._run('tar() { return 0; }\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Expected source dir not found', result.stderr)
        self.assertNotIn('BUILD_REACHED', result.stdout)

    def test_failed_directory_change_stops_before_build(self):
        result = self._run('cd() { return 1; }\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not enter source directory', result.stderr)
        self.assertNotIn('BUILD_REACHED', result.stdout)


class TestUpgradeVersionVerification(unittest.TestCase):
    def _run(self, output='transmission-remote 4.1.4 (build abc123)', status=0):
        body = '''source "$1"
LATEST_VERSION=4.1.4
TRANSMISSION_REMOTE=remote
version_output=$2
version_status=$3
remote() {
    [[ "$*" == --version ]] || return 99
    printf '%s\\n' "$version_output" >&2
    return "$version_status"
}
verify_install
'''
        return subprocess.run(
            ['bash', '-c', body, 'upgrade-version-test', str(SCRIPT), output, str(status)],
            capture_output=True, text=True, timeout=5)

    def test_matching_installed_version_reports_success(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Upgrade complete. Installed version: 4.1.4', result.stdout)

    def test_old_or_unexpected_installed_version_fails(self):
        for version in ('4.1.3', '4.2.0'):
            with self.subTest(version=version):
                result = self._run('transmission-remote ' + version)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('does not match expected 4.1.4', result.stderr)
                self.assertIn(version, result.stderr)
                self.assertNotIn('Upgrade complete', result.stdout)

    def test_missing_installed_version_fails(self):
        for output in ('', 'no version available'):
            with self.subTest(output=output):
                result = self._run(output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Failed to capture installed version', result.stderr)
                self.assertNotIn('Upgrade complete', result.stdout)

    def test_failed_version_command_fails_even_if_output_matches(self):
        result = self._run(status=7)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Failed to query installed version', result.stderr)
        self.assertNotIn('Upgrade complete', result.stdout)


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
