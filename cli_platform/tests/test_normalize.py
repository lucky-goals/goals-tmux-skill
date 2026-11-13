"""Normalization of the Phase 0 probe fixtures (read-only, no model calls).

Each test feeds the recorded agent output of one fixture through the pure
normalizer and checks the unified events it produces.
"""

import json
import unittest

from support import ROOT

from cli_platform.adapters.base import PreEvent, decide_permission
from cli_platform.normalize import Normalizer, turn_result

FIXTURES = ROOT.parent / "docs" / "research" / "phase0-20261113" / "fixtures"


def frames(name: str, direction: str = "rx") -> list[dict]:
    data = json.loads((FIXTURES / name).read_text())
    return [frame for frame in data["frames"] if frame["dir"] == direction]


def acp_turn(name: str) -> tuple[Normalizer, list[PreEvent]]:
    """Map every notification of an ACP fixture, as the transport would."""
    norm = Normalizer("t0001", "acp", protocol=1)
    out: list[PreEvent] = []
    for raw_seq, frame in enumerate(frames(name), start=1):
        message = frame["data"]
        if isinstance(message, dict) and "method" in message and "id" not in message:
            out += norm.acp_message(message, raw_seq, frame["t_rel_s"])
    return norm, out + norm.finish()


def headless_turn(adapter: str, name: str) -> tuple[Normalizer, list[PreEvent]]:
    norm = Normalizer("t0001", "headless")
    out: list[PreEvent] = []
    for raw_seq, frame in enumerate(frames(name), start=1):
        if isinstance(frame["data"], dict):
            out += norm.headless_line(adapter, frame["data"], raw_seq, frame["t_rel_s"])
    exit_code = json.loads((FIXTURES / name).read_text())["exit_code"]
    out += norm.finish()
    return norm, out + [norm.headless_turn_end(exit_code)]


def of_type(events: list[PreEvent], *types: str) -> list[PreEvent]:
    return [event for event in events if event.type in types]


class AcpFixtureTest(unittest.TestCase):
    def test_agy_trivial_prompt(self):
        norm, events = acp_turn("p0c_agy_acp.json")
        self.assertEqual(of_type(events, "adapter_unknown"), [])
        messages = of_type(events, "message")
        self.assertEqual([(m.payload["role"], m.payload["text"]) for m in messages],
                         [("user", "Reply with exactly: PROBE_OK"), ("assistant", "PROBE_OK")])
        self.assertEqual(norm.observed["model"], "gemini-3.8-flash")
        self.assertTrue(of_type(events, "usage"))
        kinds = {event.payload["kind"] for event in of_type(events, "session_meta")}
        self.assertLessEqual({"config", "commands", "info"}, kinds)

    def test_repeated_metadata_is_dropped_and_counted(self):
        norm, events = acp_turn("p0c_agy_acp.json")
        # The fixture repeats available_commands_update with the same list.
        self.assertEqual(len([e for e in of_type(events, "session_meta") if e.payload["kind"] == "commands"]), 1)
        self.assertGreater(norm.counters["duplicates_dropped"], 0)

    def test_kiro_vendor_notifications_are_ignored_not_unknown(self):
        norm, events = acp_turn("p0c_kiro_acp.json")
        vendor = [frame for frame in frames("p0c_kiro_acp.json")
                  if isinstance(frame["data"], dict) and str(frame["data"].get("method", "")).startswith("_kiro/")]
        self.assertEqual(len(vendor), 23)
        self.assertGreaterEqual(norm.counters["vendor_ignored"], 23)
        self.assertEqual(of_type(events, "adapter_unknown"), [])
        self.assertEqual(of_type(events, "message")[-1].payload["text"], "PROBE_OK")

    def test_kiro_v2_tool_call_permission_and_dev_notifications(self):
        norm, events = acp_turn("p0e_kiro_acp_v2_allow.json")
        self.assertEqual(of_type(events, "adapter_unknown"), [])
        call = of_type(events, "tool_call")[0].payload
        self.assertEqual((call["kind"], call["tool"]), ("execute", "shell"))
        result = of_type(events, "tool_result")[0].payload
        self.assertEqual((result["call_id"], result["ok"]), (call["call_id"], True))
        self.assertIn("PERM_PROBE_OK", result["output_summary"])
        self.assertGreater(norm.counters["vendor_ignored"], 0)   # _kiro.dev/*

    def test_kiro_v2_permission_request_is_answered_by_profile(self):
        request = next(frame["data"]["params"] for frame in frames("p0e_kiro_acp_v2_allow.json")
                       if isinstance(frame["data"], dict)
                       and frame["data"].get("method") == "session/request_permission")
        request = dict(request, toolCall=dict(request["toolCall"], kind="execute"))
        bypass = decide_permission("bypass", request)
        self.assertEqual((bypass.outcome, bypass.option_id), ("allowed", "allow_once"))
        review = decide_permission("read_only_auto", request)
        self.assertEqual((review.outcome, review.option_id), ("rejected", "reject_once"))
        read = decide_permission("read_only_auto", dict(request, toolCall={"kind": "read"}))
        self.assertEqual(read.outcome, "allowed")
        for decision in (bypass, review, read):
            self.assertNotIn(decision.option_kind, ("allow_always", "reject_always"))
        none = decide_permission("bypass", {"options": [{"optionId": "a", "kind": "allow_always"}]})
        self.assertEqual(none.wire, {"outcome": {"outcome": "cancelled"}})


class HeadlessFixtureTest(unittest.TestCase):
    def test_claude_partial_messages_close_into_one_message(self):
        norm, events = headless_turn("claude", "p0c_claude_headless_auth.json")
        self.assertEqual(of_type(events, "adapter_unknown"), [])
        deltas, messages = of_type(events, "message_delta"), of_type(events, "message")
        self.assertEqual([m.payload["text"] for m in messages], ["PROBE_OK"])
        self.assertEqual({d.payload["message_id"] for d in deltas}, {messages[0].payload["message_id"]})
        end = events[-1].payload
        self.assertEqual((end["result"], end["stop_reason"]), ("ok", "end_turn"))
        self.assertEqual(end["resume_id"], norm.observed["session_id"])
        self.assertEqual(norm.observed["model"], "claude-haiku-5-5")

    def test_claude_reads_every_result_until_eof(self):
        norm, events = headless_turn("claude", "p0f_claude_hl_subagent.json")
        self.assertEqual(norm.counters["results"], 2)
        self.assertEqual(norm.terminal["text"], "The background subagent finished and returned `SUBAGENT_OK`.")
        self.assertEqual(events[-1].payload["results"], 2)
        texts = [m.payload["text"] for m in of_type(events, "message")]
        self.assertIn("SUBAGENT_OK", texts)
        self.assertEqual(texts[-1], "The background subagent finished and returned `SUBAGENT_OK`.")
        call = of_type(events, "tool_call")[0].payload
        self.assertEqual(call["kind"], "subagent")
        self.assertEqual(of_type(events, "tool_result")[0].payload["call_id"], call["call_id"])
        self.assertEqual(of_type(events, "adapter_unknown"), [])

    def test_claude_auth_failure_is_auth_required(self):
        norm, events = headless_turn("claude", "p0c_claude_headless.json")
        self.assertEqual(norm.failure, "AUTH_REQUIRED")
        self.assertEqual(of_type(events, "error")[0].payload["code"], "AUTH_REQUIRED")
        self.assertEqual(events[-1].payload["result"], "error")

    def test_kiro_headless_envelope_maps_like_acp(self):
        norm, events = headless_turn("kiro", "p0c_kiro_headless.json")
        self.assertEqual(of_type(events, "adapter_unknown"), [])
        self.assertEqual(of_type(events, "message")[-1].payload["text"], "PROBE_OK")
        end = events[-1].payload
        self.assertEqual((end["result"], end["resume_id"]), ("ok", norm.observed["session_id"]))
        self.assertTrue(end["resume_id"].startswith("sess_"))
        self.assertGreater(norm.counters["vendor_ignored"], 0)     # session_info_update with _meta only

    def test_cursor_whole_message_closes_its_chunks(self):
        norm, events = headless_turn("cursor", "p0c_cursor_headless_auto.json")
        self.assertEqual(of_type(events, "adapter_unknown"), [])
        assistant = [m for m in of_type(events, "message") if m.payload["role"] == "assistant"]
        self.assertEqual([m.payload["text"] for m in assistant], ["PROBE_OK"])
        thought = of_type(events, "thought")[0].payload
        self.assertTrue(thought["text"].startswith("The user wants"))
        self.assertEqual(events[-1].payload["result"], "ok")

    def test_cursor_tool_call_started_and_completed_frames_normalize_cleanly(self):
        norm = Normalizer("t0001", "headless")
        lines = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "sess-cursor-tc-1",
                "model": "Auto",
                "permissionMode": "default",
            },
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": "call-cursor-1",
                "tool_call": {
                    "readToolCall": {"args": {"path": "README.md"}},
                    "hookAdditionalContexts": [],
                    "toolCallId": "call-cursor-1",
                },
            },
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": "call-cursor-1",
                "tool_call": {
                    "readToolCall": {
                        "args": {"path": "README.md"},
                        "result": {"success": {"content": "hello world"}},
                    },
                    "hookAdditionalContexts": [],
                    "toolCallId": "call-cursor-1",
                },
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
                "session_id": "sess-cursor-tc-1",
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Done.",
                "session_id": "sess-cursor-tc-1",
            },
        ]
        events: list[PreEvent] = []
        for raw_seq, line in enumerate(lines, start=1):
            events += norm.headless_line("cursor", line, raw_seq, float(raw_seq) * 0.1)
        events += norm.finish()
        events.append(norm.headless_turn_end(0))

        self.assertEqual(of_type(events, "adapter_unknown"), [])
        calls = of_type(events, "tool_call")
        results = of_type(events, "tool_result")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(calls[0].payload["call_id"], "call-cursor-1")
        self.assertEqual(calls[0].payload["tool"], "readToolCall")
        self.assertEqual(calls[0].payload["status"], "in_progress")
        self.assertEqual(results[0].payload["call_id"], "call-cursor-1")
        self.assertTrue(results[0].payload["ok"])
        self.assertEqual(results[0].payload["status"], "completed")
        self.assertIn("hello world", results[0].payload["output_summary"])
        self.assertEqual([m.payload["text"] for m in of_type(events, "message")], ["Done."])
        self.assertEqual(events[-1].payload["result"], "ok")

        # Recorded Phase 0 Cursor headless turn with real tool_call started/completed frames.
        if (FIXTURES / "p0d_cursor_acp_to_hl.json").exists():
            rec_norm, rec_events = headless_turn("cursor", "p0d_cursor_acp_to_hl.json")
            self.assertEqual(of_type(rec_events, "adapter_unknown"), [])
            rec_calls = of_type(rec_events, "tool_call")
            rec_results = of_type(rec_events, "tool_result")
            self.assertGreaterEqual(len(rec_calls), 1)
            self.assertEqual(len(rec_calls), len(rec_results))
            self.assertEqual(
                {c.payload["call_id"] for c in rec_calls},
                {r.payload["call_id"] for r in rec_results},
            )
            self.assertEqual(rec_events[-1].payload["result"], "ok")
            self.assertEqual(rec_norm.observed["session_id"], "64cf4eb7-e261-4465-9006-7cced62aeb60")

        # p0c_cursor_headless_ok.json is the free-tier stderr failure probe; ensure its rx frames have no unknowns.
        if (FIXTURES / "p0c_cursor_headless_ok.json").exists():
            _, ok_probe_events = headless_turn("cursor", "p0c_cursor_headless_ok.json")
            self.assertEqual(of_type(ok_probe_events, "adapter_unknown"), [])

    def test_agy_headless_steps(self):
        norm, events = headless_turn("agy", "p0c_agy_headless_ok.json")
        self.assertEqual(of_type(events, "adapter_unknown"), [])
        self.assertEqual(of_type(events, "message")[0].payload["text"], "PROBE_OK\n")
        self.assertEqual(events[-1].payload["resume_id"], norm.observed["session_id"])


class RulesTest(unittest.TestCase):
    def update(self, norm: Normalizer, body: dict, raw_seq: int, t: float = 0.0) -> list[PreEvent]:
        return norm.acp_message({"jsonrpc": "2.0", "method": "session/update",
                                 "params": {"sessionId": "s", "update": body}}, raw_seq, t)

    def chunk(self, text: str, **extra) -> dict:
        return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}, **extra}

    def test_chunks_coalesce_within_100ms_and_flush_before_other_events(self):
        norm = Normalizer("t0001", "acp")
        out = self.update(norm, self.chunk("a"), 1, 0.00)
        out += self.update(norm, self.chunk("b"), 2, 0.05)
        out += self.update(norm, self.chunk("c"), 3, 0.20)
        out += self.update(norm, {"sessionUpdate": "plan", "entries": []}, 4, 0.21)
        out += norm.finish()
        self.assertEqual([(e.type, e.payload.get("text")) for e in out],
                         [("message_delta", "ab"), ("message_delta", "c"), ("plan", None), ("message", "abc")])
        self.assertEqual((out[0].raw_seq, out[0].raw_seq_last, out[0].payload["chunks"]), (1, 2, 2))
        self.assertEqual(out[-1].payload["message_id"], "t0001-m1")

    def test_a_tool_call_closes_the_message_and_a_new_wire_id_starts_one(self):
        norm = Normalizer("t0001", "acp")
        out = self.update(norm, self.chunk("one", messageId="w1"), 1)
        out += self.update(norm, self.chunk("two", messageId="w2"), 2)
        out += self.update(norm, {"sessionUpdate": "tool_call", "toolCallId": "c1", "kind": "read"}, 3)
        out += self.update(norm, self.chunk("again", messageId="w1"), 4)
        out += norm.finish()
        messages = of_type(out, "message")
        self.assertEqual([m.payload["text"] for m in messages], ["one", "two", "again"])
        self.assertEqual(messages[2].payload["continues"], messages[0].payload["message_id"])
        self.assertLess(out.index(messages[1]), out.index(of_type(out, "tool_call")[0]))

    def test_update_for_an_unannounced_call_gets_a_synthetic_call(self):
        norm = Normalizer("t0001", "acp")
        out = self.update(norm, {"sessionUpdate": "tool_call_update", "toolCallId": "c9",
                                 "status": "in_progress"}, 1)
        self.assertEqual([e.type for e in out], ["tool_call", "tool_call_update"])
        self.assertTrue(out[0].payload["synthetic"])

    def test_unknown_variants_pass_through_and_vendor_methods_do_not(self):
        norm = Normalizer("t0001", "acp")
        out = self.update(norm, {"sessionUpdate": "brand_new_thing"}, 1)
        out += norm.acp_message({"jsonrpc": "2.0", "method": "_vendor/x", "params": {}}, 2, 0)
        out += norm.acp_message({"jsonrpc": "2.0", "method": "other/notice", "params": {}}, 3, 0)
        self.assertEqual([e.payload["kind"] for e in out],
                         ["session/update:brand_new_thing", "notification:other/notice"])
        self.assertEqual(out[0].raw_seq, 1)

    def test_tool_input_is_redacted_and_bounded(self):
        norm = Normalizer("t0001", "acp")
        out = self.update(norm, {"sessionUpdate": "tool_call", "toolCallId": "c1",
                                 "rawInput": {"api_key": "sk-123", "body": "x" * 2000}}, 1)
        summary = out[0].payload["input_summary"]
        self.assertNotIn("sk-123", summary)
        self.assertLessEqual(len(summary), 513)

    def test_three_unparsable_lines_in_a_row_are_counted(self):
        norm = Normalizer("t0001", "acp")
        for raw_seq in (1, 2, 3):
            self.assertEqual(norm.unparsable(raw_seq)[0].payload["kind"], "unparsable")
        self.assertEqual(norm.unparsable_run, 3)

    def test_stop_reasons(self):
        self.assertEqual([turn_result(r) for r in ("end_turn", "max_tokens", "refusal", "cancelled", "new")],
                         ["ok", "incomplete", "refused", "cancelled", "unknown"])


if __name__ == "__main__":
    unittest.main()
