from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import agentwatch
import agentwatch_core


class ConfigPathSafetyTests(unittest.TestCase):
    def test_config_dir_preserves_lexical_symlink_for_installer_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            actual = root / "actual-config"
            actual.mkdir()
            linked = root / "linked-config"
            try:
                linked.symlink_to(actual, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is unavailable on this platform")

            with mock.patch.dict(
                os.environ, {"AGENTWATCH_CONFIG_DIR": str(linked)}, clear=False
            ):
                configured = agentwatch_core.config_dir()
                paths = agentwatch.InstallPaths(home=root / "home")

            self.assertEqual(Path(os.path.abspath(linked)), configured)
            self.assertNotEqual(actual.resolve(), configured)
            with self.assertRaises(agentwatch_core.AgentWatchError):
                agentwatch.install_runtime(paths)
            self.assertEqual([], list(actual.iterdir()))

    def test_non_utf8_delivery_settings_raise_safe_agentwatch_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / agentwatch_core.SETTINGS_FILE_NAME).write_bytes(b"\xff\xfe\x00")

            with self.assertRaisesRegex(
                agentwatch_core.AgentWatchError, "unreadable or invalid"
            ):
                agentwatch_core.load_delivery_mode(root)


class MachineIdentitySafetyTests(unittest.TestCase):
    def test_non_object_machine_file_is_rejected_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "machine.json"
            original = b"[]\n"
            path.write_bytes(original)

            with self.assertRaisesRegex(
                agentwatch_core.AgentWatchError, "must be a JSON object"
            ):
                agentwatch_core.load_or_create_machine(root)

            self.assertEqual(original, path.read_bytes())

    def test_malformed_machine_file_is_rejected_without_identity_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "machine.json"
            original = b"{not-json\n"
            path.write_bytes(original)

            with self.assertRaisesRegex(
                agentwatch_core.AgentWatchError, "refusing to replace computer identity"
            ):
                agentwatch_core.load_or_create_machine(root)

            self.assertEqual(original, path.read_bytes())

    def test_existing_missing_or_invalid_computer_id_is_never_rotated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "machine.json"
            for original in (b"{}\n", b'{"computer_id":"not-a-uuid"}\n'):
                with self.subTest(payload=original):
                    path.write_bytes(original)
                    with self.assertRaisesRegex(
                        agentwatch_core.AgentWatchError, "refusing to rotate computer identity"
                    ):
                        agentwatch_core.load_or_create_machine(root)
                    self.assertEqual(original, path.read_bytes())


class LinuxCredentialBackendSafetyTests(unittest.TestCase):
    def make_store(self, root: Path) -> agentwatch_core.ComputerTokenStore:
        return agentwatch_core.ComputerTokenStore(
            "computer-linux",
            root,
            system_name="Linux",
            which=lambda command: "/usr/bin/secret-tool" if command == "secret-tool" else None,
        )

    def test_fallback_marks_newer_token_and_wins_over_stale_recovered_keyring(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            store._file_save("new-fallback-token")

            with mock.patch.object(
                store, "_linux_secret_load", return_value="old-revoked-keyring-token"
            ) as secret_load:
                loaded = store.load()

            self.assertEqual("new-fallback-token", loaded)
            secret_load.assert_not_called()
            self.assertEqual(
                agentwatch_core.LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW,
                store._linux_backend_load_strict(),
            )

    def test_successful_keyring_save_removes_stale_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            store._file_save("old-fallback-token")

            with mock.patch.object(store, "_linux_secret_save") as secret_save:
                store.save("current-keyring-token")

            secret_save.assert_called_once_with("current-keyring-token")
            self.assertFalse(store._fallback_path().exists())
            self.assertEqual(
                agentwatch_core.LINUX_BACKEND_SECRET_SERVICE,
                store._linux_backend_load_strict(),
            )

    def test_failed_keyring_save_overwrites_fallback_and_survives_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))

            with mock.patch.object(
                store,
                "_linux_secret_save",
                side_effect=agentwatch_core.AgentWatchError("unavailable"),
            ):
                store.save("rotated-token")

            with mock.patch.object(
                store, "_linux_secret_load", return_value="old-revoked-token"
            ) as secret_load:
                loaded = store.load()

            self.assertEqual("rotated-token", loaded)
            secret_load.assert_not_called()
            self.assertEqual(
                agentwatch_core.LINUX_BACKEND_PRIVATE_FILE_SECRET_SHADOW,
                store._linux_backend_load_strict(),
            )

    def test_secret_service_marker_fails_closed_when_secret_tool_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            with mock.patch.object(store, "_linux_secret_save"):
                store.save("secret-service-token")

            drifted = agentwatch_core.ComputerTokenStore(
                "computer-linux",
                root,
                system_name="Linux",
                which=lambda _command: None,
            )
            with self.assertRaisesRegex(
                agentwatch_core.AgentWatchError, "recorded Linux Secret Service"
            ):
                drifted.load_strict()
            with self.assertRaisesRegex(
                agentwatch_core.AgentWatchError, "recorded Linux Secret Service"
            ):
                drifted.delete()
            with self.assertRaisesRegex(
                agentwatch_core.AgentWatchError, "recorded Linux Secret Service"
            ):
                drifted.save("replacement-token")

            self.assertEqual(
                agentwatch_core.LINUX_BACKEND_SECRET_SERVICE,
                store._linux_backend_load_strict(),
            )
            self.assertFalse(store._fallback_path().exists())

    def test_file_backend_marker_round_trips_without_secret_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = agentwatch_core.ComputerTokenStore(
                "computer-linux",
                root,
                system_name="Linux",
                which=lambda _command: None,
            )
            store.save("private-file-token")

            self.assertEqual("private-file-token", store.load_strict())
            self.assertEqual(
                agentwatch_core.LINUX_BACKEND_PRIVATE_FILE,
                store._linux_backend_load_strict(),
            )
            store.delete()
            self.assertFalse(store._fallback_path().exists())
            self.assertFalse(store._linux_backend_path().exists())

    def test_shadow_marker_requires_secret_tool_for_strict_load_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            with mock.patch.object(
                store,
                "_linux_secret_save",
                side_effect=agentwatch_core.AgentWatchError("unavailable"),
            ):
                store.save("fallback-token")

            drifted = agentwatch_core.ComputerTokenStore(
                "computer-linux",
                root,
                system_name="Linux",
                which=lambda _command: None,
            )
            with self.assertRaises(agentwatch_core.AgentWatchError):
                drifted.load_strict()
            with self.assertRaises(agentwatch_core.AgentWatchError):
                drifted.delete()
            self.assertEqual("fallback-token", store._file_load_strict())

    def test_legacy_token_status_snapshot_does_not_create_backend_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root, root / "home")
            machine = agentwatch_core.load_or_create_machine(root)
            agentwatch_core.save_delivery_mode("agentwatch", root)
            store = self.make_store(root)
            store._file_save("legacy-fallback-token")
            self.assertFalse(store._linux_backend_path().exists())

            with mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ):
                snapshot_machine, _store, token, delivery = agentwatch._delivery_snapshot(
                    paths, mutating=False
                )

            self.assertEqual(machine["computer_id"], snapshot_machine["computer_id"])
            self.assertEqual("legacy-fallback-token", token)
            self.assertTrue(delivery["agentwatch_authenticated"])
            self.assertFalse(store._linux_backend_path().exists())

    def test_read_only_snapshot_surfaces_backend_drift_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = agentwatch.InstallPaths(root, root / "home")
            agentwatch_core.load_or_create_machine(root)
            agentwatch_core.save_delivery_mode("agentwatch", root)
            store = agentwatch_core.ComputerTokenStore(
                "computer-linux",
                root,
                system_name="Linux",
                which=lambda _command: None,
            )
            store._linux_backend_save(agentwatch_core.LINUX_BACKEND_SECRET_SERVICE)
            marker_before = store._linux_backend_path().read_bytes()

            with mock.patch.object(
                agentwatch, "ComputerTokenStore", return_value=store
            ), self.assertRaisesRegex(
                agentwatch_core.AgentWatchError, "recorded Linux Secret Service"
            ):
                agentwatch._delivery_snapshot(paths, mutating=False)

            self.assertEqual(marker_before, store._linux_backend_path().read_bytes())

    def test_strict_load_distinguishes_secret_service_outage_from_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            unavailable = mock.Mock(returncode=1, stdout="", stderr="service unavailable")

            with mock.patch.object(agentwatch_core.subprocess, "run", return_value=unavailable):
                self.assertIsNone(store.load())
                with self.assertRaisesRegex(
                    agentwatch_core.AgentWatchError, "Secret Service was unavailable"
                ):
                    store.load_strict()

    def test_secret_service_clear_failure_preserves_fallback_and_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            store._file_save("must-remain-until-clear-is-certain")
            unavailable = mock.Mock(returncode=1, stdout="", stderr="service unavailable")

            with mock.patch.object(agentwatch_core.subprocess, "run", return_value=unavailable):
                with self.assertRaisesRegex(
                    agentwatch_core.AgentWatchError, "could not clear"
                ):
                    store.delete()

            self.assertEqual(
                "must-remain-until-clear-is-certain", store._file_load_strict()
            )


class StrictCredentialLoadTests(unittest.TestCase):
    def test_macos_backend_outage_is_visible_to_strict_load(self) -> None:
        backend = mock.Mock()
        backend.load.side_effect = agentwatch_core.AgentWatchError("keychain locked")
        store = agentwatch_core.ComputerTokenStore(
            "computer-mac",
            Path("/tmp/not-used"),
            system_name="Darwin",
            macos_keychain=backend,
        )

        self.assertIsNone(store.load())
        with self.assertRaisesRegex(agentwatch_core.AgentWatchError, "keychain locked"):
            store.load_strict()

    def test_windows_corrupt_dpapi_file_is_visible_to_strict_load(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = agentwatch_core.ComputerTokenStore(
                "computer-win", root, system_name="Windows"
            )
            store._windows_path().write_bytes(b"not-base64")

            self.assertIsNone(store.load())
            with self.assertRaisesRegex(
                agentwatch_core.AgentWatchError, "DPAPI computer token is unreadable"
            ):
                store.load_strict()

if __name__ == "__main__":
    unittest.main()
