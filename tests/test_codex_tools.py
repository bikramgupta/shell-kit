import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analyzer = load_module(
    "codex_session_analyzer",
    ROOT / ".codex" / "tools" / "session-analyzer" / "parser.py",
)
merge_config = load_module(
    "codex_merge_config",
    ROOT / ".codex" / "tools" / "merge_config.py",
)


def write_rollout(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def records(session_id, *, parent_id=None, model="gpt-5", input_tokens=100, output_tokens=10):
    meta = {
        "id": session_id,
        "cwd": "/tmp/project",
        "cli_version": "0.144.0",
        "source": "cli",
        "thread_source": "user",
    }
    if parent_id:
        meta.update(
            {
                "parent_thread_id": parent_id,
                "thread_source": "subagent",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "depth": 1,
                            "agent_nickname": "Lovelace",
                            "agent_role": "explorer",
                        }
                    }
                },
            }
        )
    total = input_tokens + output_tokens
    return [
        {"timestamp": "2026-07-10T10:00:00Z", "type": "session_meta", "payload": meta},
        {
            "timestamp": "2026-07-10T10:00:01Z",
            "type": "turn_context",
            "payload": {"model": model, "cwd": "/tmp/project"},
        },
        {
            "timestamp": "2026-07-10T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": input_tokens // 2,
                        "output_tokens": output_tokens,
                        "reasoning_output_tokens": output_tokens // 2,
                        "total_tokens": total,
                    },
                    "last_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": input_tokens // 2,
                        "output_tokens": output_tokens,
                        "reasoning_output_tokens": output_tokens // 2,
                        "total_tokens": total,
                    },
                },
            },
        },
    ]


class PricingTests(unittest.TestCase):
    def test_cached_and_reasoning_tokens_are_not_double_counted(self):
        usage = {
            "input_tokens": 1000,
            "cached_input_tokens": 400,
            "output_tokens": 200,
            "reasoning_output_tokens": 50,
            "total_tokens": 1200,
        }
        self.assertAlmostEqual(analyzer.estimate_cost("gpt-5", usage), 0.0028)

    def test_private_model_price_is_not_guessed(self):
        self.assertIsNone(
            analyzer.estimate_cost(
                "gpt-5.6-sol",
                {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            )
        )


class ParserTests(unittest.TestCase):
    def test_current_messages_tools_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-test.jsonl"
            data = records("root", model="gpt-5", input_tokens=100, output_tokens=10)
            data[2:2] = [
                {
                    "timestamp": "2026-07-10T10:00:01.1Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    },
                },
                {
                    "timestamp": "2026-07-10T10:00:01.2Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "hello"},
                },
                {
                    "timestamp": "2026-07-10T10:00:01.3Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "call-1",
                        "name": "apply_patch",
                        "input": "*** Begin Patch",
                        "status": "completed",
                    },
                },
                {
                    "timestamp": "2026-07-10T10:00:01.4Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-1",
                        "output": json.dumps({"exit_code": 0, "output": "Done"}),
                    },
                },
                {
                    "timestamp": "2026-07-10T10:00:01.5Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                },
                {
                    "timestamp": "2026-07-10T10:00:01.6Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "done"},
                },
            ]
            write_rollout(path, data)
            session = analyzer.parse_session(path)

            self.assertEqual(session["user_messages"], ["hello"])
            self.assertEqual(session["assistant_messages"], ["done"])
            self.assertEqual(session["counts_by_event"][analyzer.EVENT_USER], 1)
            self.assertEqual(session["counts_by_event"][analyzer.EVENT_TEXT], 1)
            self.assertEqual(session["tool_counts"]["apply_patch"], 1)
            self.assertEqual(session["token_usage"]["total_tokens"], 110)
            outputs = [
                event
                for event in session["events"]
                if event["event_type"] == analyzer.EVENT_POST_TOOL
            ]
            self.assertEqual(outputs[0]["name"], "apply_patch")
            self.assertFalse(outputs[0]["error"])

    def test_parent_child_topology_and_archived_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root_path = home / "sessions" / "2026" / "07" / "10" / "rollout-root.jsonl"
            child_path = home / "archived_sessions" / "rollout-child.jsonl"
            write_rollout(root_path, records("root", input_tokens=100, output_tokens=10))
            write_rollout(
                child_path,
                records("child", parent_id="root", input_tokens=50, output_tokens=5),
            )
            paths = list(
                analyzer.iter_session_files(
                    home / "sessions", home / "archived_sessions"
                )
            )
            self.assertEqual(len(paths), 2)
            sessions = [analyzer.parse_session(path) for path in paths]
            root = next(session for session in sessions if session["id"] == "root")
            combined = analyzer.combine_thread(root, sessions)
            self.assertEqual(combined["agent_count"], 2)
            self.assertEqual(combined["token_usage"]["total_tokens"], 165)
            self.assertEqual(combined["agents"][1]["label"], "Lovelace")
            self.assertTrue(combined["agents"][1]["path"].endswith("rollout-child.jsonl"))


class MergeConfigTests(unittest.TestCase):
    def test_merge_preserves_unmanaged_codex_state(self):
        source = (ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
        target = """model = "old"
model_reasoning_effort = "low"
notify = ["helper"]

[mcp_servers.docs]
url = "https://example.com"

[otel]
exporter = "none"

[projects."/work"]
trust_level = "trusted"
"""
        result = merge_config.merge(source, target)
        self.assertIn('model = "gpt-5.6-sol"', result)
        self.assertIn('notify = ["helper"]', result)
        self.assertIn("[mcp_servers.docs]", result)
        self.assertIn('[projects."/work"]', result)
        self.assertEqual(result.count("[otel]"), 1)
        self.assertNotIn('model = "old"', result)

    def test_cli_lists_active_and_archived_root_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            write_rollout(
                home / "sessions" / "2026" / "07" / "10" / "rollout-root.jsonl",
                records("root"),
            )
            write_rollout(
                home / "archived_sessions" / "rollout-old.jsonl",
                records("old"),
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / ".codex" / "tools" / "session-analyzer" / "parser.py"),
                    "--codex-home",
                    str(home),
                    "--list",
                    "--output",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual({session["id"] for session in payload}, {"root", "old"})


if __name__ == "__main__":
    unittest.main()
