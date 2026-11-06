"""Focused contract tests for the bounded SSH bridge."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from runtime.goals_tmux.errors import DomainError
from runtime.goals_tmux.remote import SshEndpoint, _ssh_argv, forward, validate_bridge


class RemoteBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="goals-ssh-bridge-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def fake_ssh(self, source: str) -> str:
        executable = self.root / "fake ssh"
        executable.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        executable.chmod(0o700)
        return str(executable)

    @staticmethod
    def invoke_request(operation: str = "send_text") -> dict[str, object]:
        arguments = {"text": "hello"} if operation == "send_text" else {}
        return {
            "schema_version": 1,
            "request_id": "req-remote-1",
            "operation": operation,
            "arguments": arguments,
        }

    def test_host_injection_is_rejected_and_runner_is_one_quoted_token(self) -> None:
        with self.assertRaises(ValueError):
            SshEndpoint("host; touch /tmp/injected", "/usr/local/bin/goals-tmux")
        with self.assertRaises(ValueError):
            SshEndpoint("-oProxyCommand=bad", "goals-tmux")
        with self.assertRaises(ValueError):
            SshEndpoint("host", "~/.local/bin/goals-tmux")

        runner = "/opt/goals tools/goals-tmux; printf injected"
        endpoint = SshEndpoint(
            "dev@example.invalid",
            runner,
            port=2222,
            config=self.root / "ssh config",
            ssh_binary="/usr/bin/ssh",
        )
        command = _ssh_argv(endpoint)
        self.assertEqual("/usr/bin/ssh", command[0])
        self.assertIn("-T", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertEqual(str((self.root / "ssh config").resolve()), command[command.index("-F") + 1])
        self.assertEqual("2222", command[command.index("-p") + 1])
        self.assertEqual("dev@example.invalid", command[-2])
        self.assertEqual(shlex.quote(runner) + " bridge", command[-1])

    def test_namespace_is_stable_and_changes_with_connection_identity(self) -> None:
        first = SshEndpoint("dev@example.invalid", "goals-tmux", config=self.root / "ssh_config")
        same = SshEndpoint("dev@example.invalid", "another-runner", config=self.root / "ssh_config")
        other_host = SshEndpoint("other@example.invalid", "goals-tmux", config=self.root / "ssh_config")
        other_port = SshEndpoint("dev@example.invalid", "goals-tmux", port=2202,
                                 config=self.root / "ssh_config")
        self.assertEqual(first.namespace, same.namespace)
        self.assertRegex(first.namespace, r"^ssh-[0-9a-f]{24}$")
        self.assertNotEqual(first.namespace, other_host.namespace)
        self.assertNotEqual(first.namespace, other_port.namespace)

    def test_bridge_accepts_remote_policy_and_stdin_request_but_never_local_file_paths(self) -> None:
        namespace = SshEndpoint("dev@example.invalid", "goals-tmux").namespace
        policy_argv, returned_namespace, stdin_json = validate_bridge(
            {
                "schema_version": 1,
                "namespace": namespace,
                "argv": ["--socket", "/remote/tmux.sock", "policy", "show"],
                "stdin": None,
            }
        )
        self.assertEqual(["--socket", "/remote/tmux.sock", "policy", "show"], policy_argv)
        self.assertEqual(namespace, returned_namespace)
        self.assertIsNone(stdin_json)

        request = self.invoke_request()
        argv, _, injected = validate_bridge(
            {
                "schema_version": 1,
                "namespace": namespace,
                "argv": ["--socket", "/remote/tmux.sock", "invoke", "--request-file", "-"],
                "stdin": request,
            }
        )
        self.assertEqual("-", argv[-1])
        self.assertEqual(request, injected)
        context_argv, _, context_json = validate_bridge(
            {
                "schema_version": 1,
                "namespace": namespace,
                "argv": ["registry", "retain", "--record-id", "record-1", "--context-file", "-",
                         "--reason", "keep for review"],
                "stdin": {"task_id": "task-1", "coordinator_id": "coordinator-1"},
            }
        )
        self.assertEqual("-", context_argv[context_argv.index("--context-file") + 1])
        self.assertEqual("task-1", context_json["task_id"])
        with self.assertRaises(DomainError):
            validate_bridge(
                {
                    "schema_version": 1,
                    "namespace": namespace,
                    "argv": ["invoke", "--request-file", "/local/private/request.json"],
                    "stdin": None,
                }
            )

    def test_bridge_rejects_recursion_unknown_verbs_and_transport_flags(self) -> None:
        namespace = SshEndpoint("host", "goals-tmux").namespace
        for argv in (
            ["--remote-runner"],
            ["--ssh-host", "other", "doctor"],
            ["--remote", "other-runner", "doctor"],
            ["--state-dir", "/remote/state", "invented-command"],
            ["doctor", "--unrecognized"],
            ["operation-status", "--request", "short-option-id"],
        ):
            with self.subTest(argv=argv), self.assertRaises(DomainError):
                validate_bridge({"schema_version": 1, "namespace": namespace, "argv": argv, "stdin": None})

    def test_forward_streams_request_file_as_json_and_preserves_remote_namespace(self) -> None:
        request = self.invoke_request()
        response = {
            "schema_version": 1,
            "request_id": request["request_id"],
            "status": "ok",
            "code": "OK",
            "data": None,
            "side_effects": "confirmed",
            "warnings": [],
        }
        program = (
            "import json,sys\n"
            "envelope=json.load(sys.stdin)\n"
            "assert envelope['schema_version']==1\n"
            "assert envelope['argv'][-1]=='-'\n"
            "assert envelope['stdin']['request_id']=='req-remote-1'\n"
            "assert envelope['namespace'].startswith('ssh-')\n"
            f"sys.stdout.write({json.dumps(json.dumps(response, separators=(',', ':')))})\n"
        )
        endpoint = SshEndpoint("dev@example.invalid", "goals-tmux", ssh_binary=self.fake_ssh(program))
        result = forward(
            endpoint,
            ["--state-dir", "/remote/state", "invoke", "--request-file", "-"],
            json.dumps(request).encode("utf-8"),
            mutating=True,
        )
        self.assertEqual("ok", result["status"])
        self.assertEqual("req-remote-1", result["request_id"])

    def test_mutating_transport_failure_is_unknown_even_if_caller_marks_it_read_only(self) -> None:
        endpoint = SshEndpoint(
            "dev@example.invalid",
            "goals-tmux",
            ssh_binary=self.fake_ssh("import sys\nsys.stdin.buffer.read()\nsys.exit(255)\n"),
        )
        request = self.invoke_request()
        result = forward(
            endpoint,
            ["invoke", "--request-file", "-"],
            json.dumps(request).encode(),
            mutating=False,
        )
        self.assertEqual("unknown", result["status"])
        self.assertEqual("unknown", result["side_effects"])
        self.assertEqual("req-remote-1", result["request_id"])

    def test_read_only_transport_failure_is_refused_without_side_effects(self) -> None:
        endpoint = SshEndpoint(
            "dev@example.invalid",
            "goals-tmux",
            ssh_binary=self.fake_ssh("import sys\nsys.stdin.buffer.read()\nsys.exit(255)\n"),
        )
        result = forward(endpoint, ["policy", "show"], None, mutating=False)
        self.assertEqual("refused", result["status"])
        self.assertEqual("none", result["side_effects"])

    def test_mismatched_response_request_id_is_unknown_for_mutation(self) -> None:
        wrong = {
            "schema_version": 1,
            "request_id": "another-request",
            "status": "ok",
            "code": "OK",
            "data": {},
            "side_effects": "confirmed",
            "warnings": [],
        }
        endpoint = SshEndpoint(
            "host",
            "goals-tmux",
            ssh_binary=self.fake_ssh(
                f"import sys\nsys.stdin.buffer.read()\nsys.stdout.write({json.dumps(json.dumps(wrong))})\n"
            ),
        )
        result = forward(endpoint, ["task", "create", "--task-id", "t", "--goal", "g", "--cwd", "/r",
                                     "--coordinator-id", "c"], None, mutating=True, request_id="expected")
        self.assertEqual("unknown", result["status"])
        self.assertEqual("REMOTE_RESPONSE_INVALID", result["code"])

    def test_operation_status_request_id_is_correlated_without_explicit_id(self) -> None:
        response = {
            "schema_version": 1,
            "request_id": "request-to-check",
            "status": "ok",
            "code": "OK",
            "data": {"status": "completed"},
            "side_effects": "none",
            "warnings": [],
        }
        program = (
            "import json,sys\n"
            "envelope=json.load(sys.stdin)\n"
            "argv=envelope['argv']\n"
            "assert argv[0]=='operation-status'\n"
            "assert argv[2]=='request-to-check'\n"
            f"sys.stdout.write({json.dumps(json.dumps(response, separators=(',', ':')))})\n"
        )
        endpoint = SshEndpoint("host", "goals-tmux", ssh_binary=self.fake_ssh(program))
        result = forward(endpoint, ["operation-status", "--request-id", "request-to-check"], None,
                         mutating=False)
        self.assertEqual("ok", result["status"])
        self.assertEqual("request-to-check", result["request_id"])

    def test_response_limit_and_timeout_are_bounded_and_uncertain(self) -> None:
        endpoint = SshEndpoint("host", "goals-tmux", ssh_binary=self.fake_ssh(
            "import sys\nsys.stdin.buffer.read()\nsys.stdout.write('x'*128)\n"
        ))
        with patch("runtime.goals_tmux.remote.MAX_RESPONSE_BYTES", 64):
            result = forward(endpoint, ["task", "create", "--task-id", "t", "--goal", "g", "--cwd", "/r",
                                        "--coordinator-id", "c"], None, mutating=True, request_id="r")
        self.assertEqual("unknown", result["status"])
        self.assertEqual("REMOTE_RESPONSE_TOO_LARGE", result["code"])

        slow = SshEndpoint("host", "goals-tmux", ssh_binary=self.fake_ssh(
            "import sys,time\nsys.stdin.buffer.read()\ntime.sleep(2)\n"
        ))
        timed_out = forward(slow, ["task", "create", "--task-id", "t", "--goal", "g", "--cwd", "/r",
                                   "--coordinator-id", "c"], None, mutating=True, request_id="r", timeout=0.05)
        self.assertEqual("unknown", timed_out["status"])
        self.assertEqual("REMOTE_TIMEOUT", timed_out["code"])

    def test_timeout_and_stdin_contract_are_validated_before_starting_ssh(self) -> None:
        endpoint = SshEndpoint("host", "goals-tmux", ssh_binary="/does/not/exist")
        result = forward(endpoint, ["doctor"], None, mutating=False, timeout=331)
        self.assertEqual("refused", result["status"])
        self.assertEqual("REMOTE_TIMEOUT_INVALID", result["code"])

        malformed_timeout = forward(endpoint, ["doctor"], None, mutating=False, timeout="bad")
        self.assertEqual("REMOTE_TIMEOUT_INVALID", malformed_timeout["code"])

        no_stdin = forward(endpoint, ["invoke", "--request-file", "-"], None, mutating=True)
        self.assertEqual("refused", no_stdin["status"])
        self.assertEqual("none", no_stdin["side_effects"])


if __name__ == "__main__":
    unittest.main()
