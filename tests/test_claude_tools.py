import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".claude" / "tools" / "session-analyzer"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# narrative.py imports `parser` from its own directory.
sys.path.insert(0, str(TOOLS))
analyzer = load_module("claude_session_analyzer", TOOLS / "parser.py")
sys.modules["parser"] = analyzer
narrative = load_module("claude_session_narrative", TOOLS / "narrative.py")


USAGE = {
    "input_tokens": 2,
    "output_tokens": 500,
    "cache_creation_input_tokens": 1000,
    "cache_read_input_tokens": 40000,
}


def assistant_entry(uuid, msg_id, block, parent=None, request="req_1"):
    """One content block of an API response.

    Claude Code writes each block as its own transcript entry, and every one
    repeats the same usage object.
    """
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "requestId": request,
        "timestamp": "2026-01-01T00:00:0%s.000Z" % uuid[-1],
        "cwd": "/tmp/proj",
        "sessionId": "s1",
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-8",
            "role": "assistant",
            "content": [block],
            "usage": dict(USAGE),
        },
    }


def one_response_split_across_entries():
    """A single API response: thinking + text + tool_use = 3 entries, 1 usage."""
    return [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "promptId": "p1",
            "timestamp": "2026-01-01T00:00:00.000Z",
            "cwd": "/tmp/proj",
            "sessionId": "s1",
            "message": {"role": "user", "content": "do the thing"},
        },
        assistant_entry("a1", "msg_1", {"type": "thinking", "thinking": "hmm"}, "u1"),
        assistant_entry("a2", "msg_1", {"type": "text", "text": "Working on it."}, "u1"),
        assistant_entry("a3", "msg_1", {
            "type": "tool_use", "id": "t1", "name": "Bash",
            "input": {"command": "ls"},
        }, "u1"),
    ]


class UsageDedupTests(unittest.TestCase):
    """One API response must be counted once, however many entries it spans."""

    def test_usage_key_groups_entries_of_one_response(self):
        entries = one_response_split_across_entries()[1:]
        keys = {analyzer.usage_key(e) for e in entries}
        self.assertEqual(keys, {"msg_1"})

    def test_aggregate_counts_each_response_once(self):
        agg = analyzer.aggregate_tokens(one_response_split_across_entries())
        self.assertEqual(agg["totals"]["output_tokens"], USAGE["output_tokens"])
        self.assertEqual(agg["totals"]["cache_read_input_tokens"],
                         USAGE["cache_read_input_tokens"])
        self.assertEqual(agg["total_tokens"], sum(USAGE.values()))

    def test_distinct_responses_still_add_up(self):
        entries = one_response_split_across_entries()
        entries.append(assistant_entry("a4", "msg_2",
                                       {"type": "text", "text": "Done."}, "u1",
                                       request="req_2"))
        agg = analyzer.aggregate_tokens(entries)
        self.assertEqual(agg["totals"]["output_tokens"], USAGE["output_tokens"] * 2)

    def test_narrative_matches_analyzer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s1.jsonl"
            path.write_text("".join(
                json.dumps(r) + "\n" for r in one_response_split_across_entries()))
            data = narrative.build(path)
            agg = analyzer.aggregate_tokens(narrative.load(path))

        self.assertEqual(data["tokens"]["out"], agg["totals"]["output_tokens"])
        self.assertEqual(data["tokens"]["cache_read"],
                         agg["totals"]["cache_read_input_tokens"])
        self.assertEqual(sum(t["api_requests"] for t in data["turns"]), 1)


class NarrativeStructureTests(unittest.TestCase):
    def test_turn_groups_assistant_entries_with_their_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s1.jsonl"
            path.write_text("".join(
                json.dumps(r) + "\n" for r in one_response_split_across_entries()))
            data = narrative.build(path)

        self.assertEqual(len(data["turns"]), 1)
        turn = data["turns"][0]
        self.assertEqual(turn["prompt"], "do the thing")
        self.assertEqual(turn["prompt_kind"], "human")
        # thinking + text + the unresolved tool call
        self.assertEqual([s["t"] for s in turn["steps"]],
                         ["think", "say", "tool"])

    def test_failed_call_then_same_target_is_a_retry(self):
        tools = [
            {"tool": "Edit", "call": "/a/b.py", "ok": False},
            {"tool": "Edit", "call": "/a/b.py", "ok": True},
        ]
        narrative.infer_retries(tools)
        self.assertEqual(tools[1]["retry_of"], 0)

    def test_sibling_path_is_not_a_retry(self):
        """Sibling paths share long prefixes and must not fuzzy-match."""
        tools = [
            {"tool": "Edit", "call": "/a/session-stats.md", "ok": False},
            {"tool": "Edit", "call": "/a/session-state.md", "ok": True},
        ]
        narrative.infer_retries(tools)
        self.assertNotIn("retry_of", tools[1])


if __name__ == "__main__":
    unittest.main()
