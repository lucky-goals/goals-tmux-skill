"""Write-time secret redaction across RawLog, StreamLog, and run turns."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import make_config, raw_config

from cli_platform.config import _SECRET_NAME_RE
from cli_platform.events import EventLog, StreamLog
from cli_platform.scanner import CliScan
from cli_platform.supervisor import Platform, RawLog, RunService

TESTS = Path(__file__).resolve().parent


def _clean_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SECRET_NAME_RE.search(k)}
    if overrides:
        env.update(overrides)
    return env


class RedactTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.work = self.root / "work"
        self.work.mkdir()

    def _make_headless_service(
        self,
        frames: list[dict],
        *,
        env_allow: list[str] | tuple[str, ...] = (),
        exit_code: int = 0,
    ) -> RunService:
        fixture_path = self.root / "fixture.json"
        fixture_path.write_text(json.dumps({"exit_code": exit_code, "frames": frames}))

        wrapper = self.root / "bin" / "cl-fake"
        wrapper.parent.mkdir(exist_ok=True)
        py = shlex.quote(sys.executable)
        fake_cli = shlex.quote(str(TESTS / "fake_headless_cli.py"))
        wrapper.write_text(
            "#!/bin/sh\n"
            f"exec {py} {fake_cli} --fixture {shlex.quote(str(fixture_path))} \"$@\"\n"
        )
        wrapper.chmod(0o755)

        raw = raw_config()
        raw["concurrency"].update(heartbeat_seconds=1, lease_ttl_seconds=30)
        raw["execution"] = {
            "cancel_grace_seconds": 1,
            "close_grace_seconds": 2,
            "quiet_timeout_seconds": 20,
        }
        raw["variants"] = {
            "cl": {
                "adapter": "claude",
                "binary": str(wrapper),
                "account": "acct-a",
                "transport": "headless",
                "env_allow": list(env_allow),
            }
        }
        raw["models"] = [{"variant": "cl", "id": "fake-big", "efforts": ["low", "medium", "high"]}]
        raw["stages"] = [
            {
                "id": "work",
                "permission_profile": "bypass",
                "max_parallel": 2,
                "candidates": [{"variant": "cl", "model": "fake-big", "effort": "high"}],
            }
        ]
        raw.pop("routing")
        config = make_config(self.root, raw)
        scans = [
            CliScan(
                adapter="claude",
                installed=True,
                binary=str(wrapper),
                path=str(wrapper),
                realpath=str(wrapper),
                version="1.0",
            )
        ]
        return RunService(Platform(config, scans), poll_seconds=0.1)

    def test_raw_log_and_stream_log_scrub_tokens_headers_urls_secret_keys_and_env_allow_values(self):
        import cli_platform.redact  # noqa: F401

        secrets = {
            "sk_oauth": "sk-ant-oat01-secret9876543210",
            "sk_proj": "sk-proj-fakekey9876543210",
            "ghp": "ghp_FakeGitHubToken9876543210ABCDEF",
            "gh_pat": "github_pat_11FAKEPAT0123456789_secretpayload",
            "xoxb": "xoxb-111122223333-444455556666-slackbotsecret",
            "xoxp": "xoxp-111122223333-444455556666-slackusersecret",
            "xoxa": "xoxa-111122223333-slackappsecret",
            "xoxr": "xoxr-111122223333-slackrefreshsecret",
            "xoxs": "xoxs-111122223333-slacksessionsecret",
            "akia": "AKIAIOSFODNN7EXAMPLE",
            "jwt": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJqd3RzZWNyZXQ5ODc2In0.sig9876543210fake",
            "hdr_auth": "hdr-bearer-secret-9876",
            "hdr_proxy": "hdr-proxy-secret-9876",
            "hdr_xapi": "hdr-xapikey-secret-9876",
            "url_pass": "p9876secret",
            "dict_apikey": "dict-apikey-secret-9876",
            "dict_token": "dict-token-secret-9876",
            "dict_pass": "dict-password-secret-9876",
            "env_secret_name": "runtime-env-secret-9876",
            "env_allow_opaque": "variant-allow-secret-9876",
        }
        leak_text = " ".join([
            f"CLAUDE_CODE_OAUTH_TOKEN={secrets['sk_oauth']}",
            secrets["sk_proj"],
            secrets["ghp"],
            secrets["gh_pat"],
            secrets["xoxb"],
            secrets["xoxp"],
            secrets["xoxa"],
            secrets["xoxr"],
            secrets["xoxs"],
            secrets["akia"],
            secrets["jwt"],
            f"Authorization: Bearer {secrets['hdr_auth']}",
            f"Proxy-Authorization: Basic {secrets['hdr_proxy']}",
            f"X-Api-Key: {secrets['hdr_xapi']}",
            f"https://u9876:{secrets['url_pass']}@example.com/api/v1",
            secrets["env_secret_name"],
            secrets["env_allow_opaque"],
        ])
        frames = [
            {"dir": "rx", "data": {"type": "system", "subtype": "init", "session_id": "sess-1", "model": "fake-big"}},
            {
                "dir": "rx",
                "data": {
                    "type": "assistant",
                    "message": {
                        "id": "msg-1",
                        "content": [
                            {"type": "text", "text": leak_text},
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "Bash",
                                "input": {
                                    "api_key": secrets["dict_apikey"],
                                    "access_token": secrets["dict_token"],
                                    "password": secrets["dict_pass"],
                                    "command": "echo ok",
                                },
                            },
                        ],
                    },
                },
            },
            {
                "dir": "rx",
                "data": {
                    "type": "result",
                    "subtype": "success",
                    "stop_reason": "end_turn",
                    "session_id": "sess-1",
                    "result": "done",
                },
            },
            {"dir": "stderr", "data": f"stderr leak: {secrets['sk_oauth']} {secrets['env_allow_opaque']}"},
        ]

        env = _clean_env({
            "CLAUDE_CODE_OAUTH_TOKEN": secrets["sk_oauth"],
            "TEST_RUNTIME_SECRET": secrets["env_secret_name"],
            "CUSTOM_VARIANT_FLAG": secrets["env_allow_opaque"],
        })
        with mock.patch.dict(os.environ, env, clear=True):
            service = self._make_headless_service(
                frames,
                env_allow=["CLAUDE_CODE_OAUTH_TOKEN", "CUSTOM_VARIANT_FLAG"],
            )
            run = service.create(stage="work", cwd=str(self.work))["run"]
            result = service.send(run["run_id"], "say hello")
            self.assertEqual(result["state"], "idle")

            handle = service.find(run["run_id"])
            raw_text = handle.raw_path.read_text()
            events_text = handle.events_path.read_text()

            # Also verify direct RawLog and non-run StreamLog writes scrub secrets.
            direct_raw = RawLog(self.root / "direct" / "raw.jsonl", "t9999")
            direct_raw.append("rx", f"Bearer {secrets['hdr_auth']} {secrets['env_secret_name']}")
            direct_raw_text = direct_raw.path.read_text()

            limits_log = StreamLog(self.root / "direct" / "limits.jsonl", stream="limits")
            limits_log.append(
                "lease_granted",
                {
                    "api_key": secrets["dict_apikey"],
                    "note": f"https://u9876:{secrets['url_pass']}@example.com/v1 {secrets['jwt']}",
                },
            )
            limits_text = limits_log.path.read_text()

        forbidden_substrings = [
            "secret9876543210",
            "fakekey9876543210",
            "FakeGitHubToken9876543210ABCDEF",
            "11FAKEPAT0123456789_secretpayload",
            "slackbotsecret",
            "slackusersecret",
            "slackappsecret",
            "slackrefreshsecret",
            "slacksessionsecret",
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "eyJzdWIiOiJqd3RzZWNyZXQ5ODc2In0",
            "hdr-bearer-secret-9876",
            "hdr-proxy-secret-9876",
            "hdr-xapikey-secret-9876",
            "u9876:p9876secret",
            "p9876secret",
            "dict-apikey-secret-9876",
            "dict-token-secret-9876",
            "dict-password-secret-9876",
            "runtime-env-secret-9876",
            "variant-allow-secret-9876",
        ]
        for label, content in (
            ("raw.jsonl", raw_text),
            ("events.jsonl", events_text),
            ("direct raw.jsonl", direct_raw_text),
            ("limits.jsonl", limits_text),
        ):
            for needle in forbidden_substrings:
                self.assertNotIn(needle, content, f"{needle!r} leaked into {label}")

    def test_written_events_record_exact_integer_redactions_count(self):
        import cli_platform.redact  # noqa: F401

        frames = [
            {"dir": "rx", "data": {"type": "system", "subtype": "init", "session_id": "sess-1", "model": "fake-big"}},
            {
                "dir": "rx",
                "data": {
                    "type": "assistant",
                    "message": {
                        "id": "msg-1",
                        "content": [{"type": "text", "text": "token=sk-ant-oat01-secret9876543210"}],
                    },
                },
            },
            {
                "dir": "rx",
                "data": {
                    "type": "result",
                    "subtype": "success",
                    "stop_reason": "end_turn",
                    "session_id": "sess-1",
                    "result": "done",
                },
            },
        ]
        with mock.patch.dict(os.environ, _clean_env(), clear=True):
            service = self._make_headless_service(frames)
            run = service.create(stage="work", cwd=str(self.work))["run"]
            service.send(run["run_id"], "clean instruction")
            handle = service.find(run["run_id"])

            events = [json.loads(line) for line in handle.events_path.read_text().splitlines()]
            self.assertTrue(events)
            for ev in events:
                self.assertIn("redactions", ev)
                self.assertIs(type(ev["redactions"]), int)

            msg_events = [ev for ev in events if ev["type"] == "message"]
            self.assertEqual(len(msg_events), 1)
            self.assertGreaterEqual(msg_events[0]["redactions"], 1)

            clean_events = [ev for ev in events if ev["type"] in ("nickname_assigned", "route_resolved", "lease_granted")]
            self.assertTrue(clean_events)
            for ev in clean_events:
                self.assertEqual(ev["redactions"], 0)

            # Direct StreamLog check: 0 when clean, >= 1 when scrubbed.
            log = EventLog(self.root / "direct-events.jsonl", run_id="run-0001", task_id="T1", nickname="kedo")
            clean = log.append("heartbeat", {"ok": True, "path": "/tmp/work/file.py"})
            dirty = log.append("heartbeat", {"api_key": "topsecret123", "msg": "https://u:p@example.com"})
            written = [json.loads(line) for line in log.path.read_text().splitlines()]
            self.assertIs(type(clean["redactions"]), int)
            self.assertEqual(clean["redactions"], 0)
            self.assertIs(type(dirty["redactions"]), int)
            self.assertGreaterEqual(dirty["redactions"], 1)
            self.assertEqual([w["redactions"] for w in written], [clean["redactions"], dirty["redactions"]])

    def test_sha256_digests_run_ids_paths_and_short_env_values_under_8_chars_are_not_false_redacted(self):
        import cli_platform.redact  # noqa: F401

        sha256_hex = hashlib.sha256(b"clean-message-payload").hexdigest()
        run_id = "run-019A1B2C3D4E5F6789"
        file_path = "/Users/dev/workspace/goals-tmux-skill/cli_platform/src/cli_platform/events.py"
        short_secret_env = "tok1234"   # len == 7 (< 8)
        short_allow_env = "abc_xyz"    # len == 7 (< 8)
        clean_text = f"run={run_id} digest={sha256_hex} path={file_path} short={short_secret_env} allow={short_allow_env}"

        frames = [
            {"dir": "rx", "data": {"type": "system", "subtype": "init", "session_id": "sess-1", "model": "fake-big"}},
            {
                "dir": "rx",
                "data": {
                    "type": "assistant",
                    "message": {
                        "id": "msg-1",
                        "content": [{"type": "text", "text": clean_text}],
                    },
                },
            },
            {
                "dir": "rx",
                "data": {
                    "type": "result",
                    "subtype": "success",
                    "stop_reason": "end_turn",
                    "session_id": "sess-1",
                    "result": "ok",
                },
            },
        ]
        env = _clean_env({
            "SHORT_API_TOKEN": short_secret_env,
            "SHORT_ALLOW_ENV": short_allow_env,
        })
        with mock.patch.dict(os.environ, env, clear=True):
            service = self._make_headless_service(frames, env_allow=["SHORT_ALLOW_ENV"])
            run = service.create(stage="work", cwd=str(self.work), run_id=run_id)["run"]
            service.send(run["run_id"], "inspect files")

            handle = service.find(run_id)
            raw_text = handle.raw_path.read_text()
            events = [json.loads(line) for line in handle.events_path.read_text().splitlines()]

            for ev in events:
                self.assertEqual(ev["redactions"], 0, f"unexpected redaction on {ev}")

            msg = next(ev for ev in events if ev["type"] == "message")
            self.assertEqual(msg["payload"]["text"], clean_text)
            self.assertEqual(msg["payload"]["sha256"], hashlib.sha256(clean_text.encode()).hexdigest())

            for expected in (sha256_hex, run_id, file_path, short_secret_env, short_allow_env):
                self.assertIn(expected, raw_text)
                self.assertIn(expected, msg["payload"]["text"])


if __name__ == "__main__":
    unittest.main()
