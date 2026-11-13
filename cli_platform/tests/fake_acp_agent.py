#!/usr/bin/env python3
"""A scripted ACP agent (JSON-RPC 2.0 over stdio) for the transport tests.

    fake_acp_agent.py [--scenario NAME] [--record FILE] [anything else...]

Message shapes follow the Phase 0 fixtures (agy and Kiro). No model, no
network. Scenarios:

    ok            prompt streams updates (with a Kiro-style vendor notification) and ends
    permission    asks session/request_permission (--tool-kind) and echoes the answer
    slow          streams chunks until session/cancel, then stops with "cancelled"
    ignore_cancel streams chunks and never acknowledges a cancel
    hang_init     never answers initialize
    bad_version   answers initialize with protocol version 99
    auth          answers initialize with JSON-RPC error -32000
    crash         exits in the middle of the prompt
    garbage       writes unparsable lines during the prompt
    not_found     session/resume and session/load answer -32602 "Session ... not found"
    load_only     advertises loadSession but not resume; session/load replays history
    no_eof_exit   behaves like "ok" but does not exit on stdin EOF (Cursor, Kiro v3)
"""

import argparse
import json
import queue
import sys
import threading
import time

MODELS = ["fake-model", "fake-big"]
LEVELS = ["low", "medium", "high"]


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--scenario", default="ok")
    parser.add_argument("--record")
    parser.add_argument("--tool-kind", default="execute")
    args, _ = parser.parse_known_args()
    scenario = args.scenario
    inbox: queue.Queue = queue.Queue()

    def read() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            if args.record:
                with open(args.record, "a") as handle:
                    handle.write(json.dumps(message) + "\n")
            inbox.put(message)
        inbox.put(None)

    threading.Thread(target=read, daemon=True).start()
    config = {"model": "fake-model", "thought_level": "medium"}
    session = "sess-fake-1"

    def send(message: dict) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def update(body: dict) -> None:
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": session, "update": body}})

    def options() -> list:
        return [{"id": "model", "name": "Model", "category": "model", "type": "select",
                 "currentValue": config["model"],
                 "options": [{"value": value, "name": value} for value in MODELS]},
                {"id": "thought_level", "name": "Thinking", "category": "thought_level",
                 "type": "select", "currentValue": config["thought_level"],
                 "options": [{"value": value, "name": value} for value in LEVELS]}]

    def chunk(text: str) -> None:
        update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

    def wait_cancel(timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = inbox.get(timeout=0.01)
            except queue.Empty:
                return False
            if message is None:
                return False
            if message.get("method") == "session/cancel":
                return True
        return False

    while True:
        message = inbox.get()
        if message is None:
            if scenario == "no_eof_exit":
                time.sleep(60)
            return 0
        method, rid = message.get("method"), message.get("id")
        if method == "initialize":
            if scenario == "hang_init":
                continue
            if scenario == "auth":
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Authentication required"}})
                continue
            sessions = {"close": {}, "list": {}}
            if scenario != "load_only":
                sessions["resume"] = {}
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 99 if scenario == "bad_version" else 1,
                "agentCapabilities": {"loadSession": True, "sessionCapabilities": sessions,
                                      "promptCapabilities": {"image": False}},
                "agentInfo": {"name": "fake-acp", "version": "0.0.1"}, "authMethods": []}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": session, "configOptions": options()}})
        elif method in ("session/resume", "session/load"):
            if scenario == "not_found":
                send({"jsonrpc": "2.0", "id": rid, "error": {
                    "code": -32602, "message": "Invalid params",
                    "data": {"message": f"Session \"{message['params']['sessionId']}\" not found"}}})
                continue
            session = message["params"]["sessionId"]
            if method == "session/load":
                for text in ("old question", "old answer", "more history"):
                    chunk(text)
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/set_config_option":
            config[message["params"]["configId"]] = message["params"]["value"]
            send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": options()}})
        elif method == "session/prompt":
            text = message["params"]["prompt"][0]["text"]
            update({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}})
            send({"jsonrpc": "2.0", "method": "_kiro/mcp/status", "params": {"servers": []}})
            if scenario == "crash":
                chunk("partial")
                sys.stdout.flush()
                return 3
            if scenario == "garbage":
                for _ in range(3):
                    sys.stdout.write("not json at all\n")
                sys.stdout.flush()
                time.sleep(5)
                return 0
            if scenario in ("slow", "ignore_cancel"):
                for index in range(400):
                    chunk(f"n{index} ")
                    if wait_cancel(0.05) and scenario == "slow":
                        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
                        break
                else:
                    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
                continue
            if scenario == "permission":
                update({"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Run echo",
                        "kind": args.tool_kind, "status": "pending",
                        "rawInput": {"command": "echo hi", "api_key": "should-be-redacted"}})
                send({"jsonrpc": "2.0", "id": "perm-1", "method": "session/request_permission",
                      "params": {"sessionId": session,
                                 "toolCall": {"toolCallId": "c1", "title": "Run echo", "kind": args.tool_kind},
                                 "options": [{"optionId": "yes", "name": "Yes", "kind": "allow_once"},
                                             {"optionId": "always", "name": "Always", "kind": "allow_always"},
                                             {"optionId": "no", "name": "No", "kind": "reject_once"}]}})
                answer = None
                while answer is None:
                    item = inbox.get()
                    if item is None:
                        return 0
                    if item.get("id") == "perm-1":
                        answer = item["result"]["outcome"]
                chunk(f"answer:{answer.get('optionId', answer['outcome'])}")
                send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
                continue
            update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}})
            chunk("Hel")
            chunk("lo")
            update({"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Read a.py", "kind": "read",
                    "status": "pending", "locations": [{"path": "/work/a.py"}]})
            update({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed",
                    "rawOutput": {"text": "print(1)"}})
            for _ in range(2):   # the second plan is identical and is dropped
                update({"sessionUpdate": "plan", "entries": [
                    {"content": "Read a.py", "priority": "high", "status": "completed"}]})
            update({"sessionUpdate": "usage_update", "used": 100, "size": 1000})
            chunk("Done.")
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn",
                                                           "usage": {"totalTokens": 12}}})
        elif rid is not None and method is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}})


if __name__ == "__main__":
    sys.exit(main())
