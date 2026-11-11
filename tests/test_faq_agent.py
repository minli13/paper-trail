import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from faq_agent import (
    DEFAULT_MODEL,
    FAQAgent,
    OpenRouterClient,
    Operation,
    run_terminal,
)


class FAQAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.documents_dir = self.root / "documents"
        self.documents_dir.mkdir()
        (self.documents_dir / "policy.md").write_text(
            "Keep records for seven years.",
            encoding="utf-8",
        )
        self.notes_dir = self.root / "notes"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_commands_from_fenced_output(self) -> None:
        operations = FAQAgent.parse_commands(
            'Plan:\n```dsl\nSEARCH("retention")\n'
            'ANSWER("How long?", "$SEARCH_RESULTS")\n```'
        )
        self.assertEqual(
            operations,
            [
                Operation("SEARCH", ("retention",)),
                Operation("ANSWER", ("How long?", "$SEARCH_RESULTS")),
            ],
        )

    def test_parser_normalizes_supported_keyword_arguments(self) -> None:
        operations = FAQAgent.parse_commands(
            'SEARCH(query="Runeterra", max_results=3)\n'
            'ANSWER(question="What is Runeterra?", passages="$SEARCH_RESULTS")'
        )

        self.assertEqual(
            operations,
            [
                Operation("SEARCH", ("Runeterra", 3)),
                Operation(
                    "ANSWER",
                    ("What is Runeterra?", "$SEARCH_RESULTS"),
                ),
            ],
        )

    def test_parser_rejects_unknown_or_duplicate_keyword_arguments(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not accept argument"):
            FAQAgent.parse_commands(
                'SEARCH(text="Runeterra")\n'
                'ANSWER("Question?", "$SEARCH_RESULTS")'
            )
        with self.assertRaisesRegex(ValueError, "more than once"):
            FAQAgent.parse_commands(
                'SEARCH("Runeterra", query="world")\n'
                'ANSWER("Question?", "$SEARCH_RESULTS")'
            )

    def test_rejects_executable_python_in_model_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "literal values"):
            FAQAgent.parse_commands(
                'SEARCH(__import__("os").getcwd())\nANSWER("Question?", "$SEARCH_RESULTS")'
            )

    def test_ask_executes_plan_and_returns_supporting_sources(self) -> None:
        prompts: list[str] = []

        def fake_llm(prompt: str) -> str:
            prompts.append(prompt)
            if "action planner" in prompt:
                return (
                    'SEARCH("seven years")\n'
                    'ANSWER("How long should records be kept?", "$SEARCH_RESULTS")'
                )
            return "Records should be kept for seven years. [1]"

        agent = FAQAgent(
            self.documents_dir,
            self.notes_dir,
            llm_call=fake_llm,
        )
        result = agent.ask("How long should records be kept?")

        self.assertEqual(
            result.answer.text,
            "Records should be kept for seven years. [1]",
        )
        self.assertEqual(result.answer.sources, ("policy.md",))
        self.assertEqual(result.citation_warnings, ())
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].source, "policy.md")
        self.assertEqual([operation.command for operation in result.operations], ["SEARCH", "ANSWER"])
        self.assertEqual(len(prompts), 2)

    def test_ask_flags_citations_that_do_not_match_supplied_sources(self) -> None:
        def fake_llm(prompt: str) -> str:
            if "action planner" in prompt:
                return (
                    'SEARCH("seven years")\n'
                    'ANSWER("How long?", "$SEARCH_RESULTS")'
                )
            return "Records should be kept for seven years. [2]"

        agent = FAQAgent(
            self.documents_dir,
            self.notes_dir,
            llm_call=fake_llm,
        )

        result = agent.ask("How long?")

        self.assertEqual(len(result.citation_warnings), 1)
        self.assertIn("2 do not match supplied sources", result.citation_warnings[0])

    def test_citation_validator_warns_when_citations_are_missing(self) -> None:
        warnings = FAQAgent.validate_citations(
            "Records should be kept for seven years.",
            ("policy.md",),
        )

        self.assertEqual(
            warnings,
            ("No numbered source citations were found in the answer.",),
        )

    def test_citation_validator_accepts_multiple_valid_sources(self) -> None:
        warnings = FAQAgent.validate_citations(
            "The answer is supported by both documents [1, 2].",
            ("policy.md", "records.md"),
        )

        self.assertEqual(warnings, ())

    def test_no_matching_passages_returns_a_no_evidence_answer(self) -> None:
        calls: list[str] = []

        def fake_llm(prompt: str) -> str:
            calls.append(prompt)
            if "action planner" in prompt:
                return 'SEARCH("unrelated")\nANSWER("Question?", "$SEARCH_RESULTS")'
            return "Should not be called"

        agent = FAQAgent(
            self.documents_dir,
            self.notes_dir,
            llm_call=fake_llm,
        )
        result = agent.ask("Question?")

        self.assertIn("couldn't find relevant passages", result.answer.text)
        self.assertEqual(result.answer.sources, ())
        self.assertEqual(len(calls), 1)

    def test_openrouter_requires_an_api_key_before_sending(self) -> None:
        client = OpenRouterClient(api_key="", model=DEFAULT_MODEL)

        with self.assertRaisesRegex(RuntimeError, "OPEN_ROUTER_API_KEY"):
            client("hello")

    def test_openrouter_retries_a_rate_limit_once(self) -> None:
        success = io.BytesIO(
            b'{"choices": [{"message": {"content": "ready"}}]}'
        )
        rate_limited = HTTPError(
            "https://openrouter.ai/api/v1/chat/completions",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b'{"error": {"message": "rate limit"}}'),
        )
        client = OpenRouterClient(
            api_key="test-key",
            model=DEFAULT_MODEL,
            max_retries=1,
            retry_delay=0.25,
        )

        with patch(
            "faq_agent.urllib.request.urlopen",
            side_effect=[rate_limited, success],
        ) as urlopen, patch("faq_agent.time.sleep") as sleep:
            result = client("test prompt")

        self.assertEqual(result, "ready")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_openrouter_reports_rate_limit_after_retry(self) -> None:
        def rate_limit() -> HTTPError:
            return HTTPError(
                "https://openrouter.ai/api/v1/chat/completions",
                429,
                "Too Many Requests",
                {},
                io.BytesIO(b'{"error": {"message": "quota exhausted"}}'),
            )

        client = OpenRouterClient(
            api_key="test-key",
            max_retries=1,
            retry_delay=0,
        )
        with patch(
            "faq_agent.urllib.request.urlopen",
            side_effect=[rate_limit(), rate_limit()],
        ) as urlopen:
            with self.assertRaisesRegex(
                RuntimeError,
                "rate limit reached \\(HTTP 429\\).*quota exhausted",
            ):
                client("test prompt")

        self.assertEqual(urlopen.call_count, 2)

    def test_terminal_asks_questions_and_displays_sources(self) -> None:
        def fake_llm(prompt: str) -> str:
            if "action planner" in prompt:
                return (
                    'SEARCH("seven years")\n'
                    'ANSWER("How long?", "$SEARCH_RESULTS")'
                )
            return "Seven years. [1]"

        agent = FAQAgent(
            self.documents_dir,
            self.notes_dir,
            llm_call=fake_llm,
        )
        inputs = iter(["How long?", "yes", "quit"])
        output: list[str] = []

        def fake_input(prompt: str) -> str:
            output.append(prompt)
            return next(inputs)

        status = run_terminal(agent, fake_input, output.append)

        self.assertEqual(status, 0)
        self.assertTrue(any("Seven years. [1]" in line for line in output))
        self.assertTrue(any("[1] policy.md" in line for line in output))
        self.assertTrue(any("Retrieved passages:" in line for line in output))
        self.assertTrue(any("Keep records for seven years." in line for line in output))
        self.assertTrue(any("Save this answer as a note?" in line for line in output))
        self.assertTrue(any("Note preview:" in line for line in output))
        self.assertEqual(
            (self.notes_dir / "How long-.md").read_text(encoding="utf-8").splitlines()[0],
            "# How long?",
        )


if __name__ == "__main__":
    unittest.main()
