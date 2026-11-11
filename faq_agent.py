"""Grounded FAQ agent using OpenRouter and the local DSL executor."""

from __future__ import annotations

import ast
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

from audit_log import AuditLogger
from dsl_executor import Answer, DSLExecutor, Passage


DEFAULT_MODEL = "recursive/flint-v1.0:free"
OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
SUPPORTED_COMMANDS = {"SEARCH", "ANSWER", "SAVE_NOTE", "LIST_DOCS"}
COMMAND_ARGUMENTS = {
    "SEARCH": ("query", "max_results"),
    "ANSWER": ("question", "passages"),
    "SAVE_NOTE": ("title", "content", "sources"),
    "LIST_DOCS": (),
}
_PLACEHOLDERS = {"$SEARCH_RESULTS", "$ANSWER", "$ANSWER_SOURCES"}


@dataclass(frozen=True)
class Operation:
    """A literal DSL operation extracted from the model's response."""

    command: str
    arguments: tuple[object, ...]
    status: str = "pending"
    feedback: str = ""
    duration_ms: float = 0.0


@dataclass(frozen=True)
class FAQResponse:
    """An answer, its evidence, citation warnings, and extracted operations."""

    answer: Answer
    operations: tuple[Operation, ...]
    evidence: tuple[Passage, ...] = ()
    citation_warnings: tuple[str, ...] = ()
    planning_duration_ms: float = 0.0


class OperationExecutionError(RuntimeError):
    """A DSL operation failed after a partial action trace was collected."""

    def __init__(
        self,
        command: str,
        operations: Sequence[Operation],
        planning_duration_ms: float,
    ) -> None:
        super().__init__(f"{command} action failed")
        self.operations = tuple(operations)
        self.planning_duration_ms = planning_duration_ms


class OpenRouterClient:
    """Minimal OpenRouter chat-completions client using Python's standard library."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = 30.0,
        max_retries: int = 1,
        retry_delay: float = 1.0,
    ) -> None:
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if retry_delay < 0:
            raise ValueError("retry_delay must be non-negative")
        self.api_key = api_key if api_key is not None else os.environ.get(
            "OPEN_ROUTER_API_KEY"
        )
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def __call__(self, prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("Set OPEN_ROUTER_API_KEY to use the OpenRouter API")
        if not prompt.strip():
            raise ValueError("OpenRouter prompt must not be empty")

        payload = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            OPENROUTER_CHAT_COMPLETIONS_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace").strip()
                if exc.code == 429:
                    if attempt < self.max_retries:
                        time.sleep(self.retry_delay)
                        continue
                    suffix = f" Details: {detail}" if detail else ""
                    raise RuntimeError(
                        "OpenRouter rate limit reached (HTTP 429). "
                        f"Please wait before trying again.{suffix}"
                    ) from exc
                raise RuntimeError(
                    f"OpenRouter returned HTTP {exc.code}: {detail}"
                ) from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"OpenRouter request failed: {exc.reason}") from exc

        try:
            result = json.loads(body)
            content = result["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenRouter returned an invalid chat completion") from exc
        if not isinstance(content, str):
            raise RuntimeError("OpenRouter returned a non-text response")
        return content


class FAQAgent:
    """Plan and execute document-grounded FAQ actions.

    Model output is treated as untrusted input. Only literal calls to the four
    DSL commands are accepted; Python expressions, attributes, and keywords
    are rejected before anything reaches the executor.
    """

    def __init__(
        self,
        documents_dir: str | Path,
        notes_dir: str | Path,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        llm_call: Callable[[str], str] | None = None,
        preview_callback: Callable[[str], None] | None = None,
        confirm_callback: Callable[[str], bool] | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self.llm_call = (
            llm_call
            if llm_call is not None
            else OpenRouterClient(api_key=api_key, model=model)
        )
        self.executor = DSLExecutor(
            documents_dir=documents_dir,
            notes_dir=notes_dir,
            llm_call=self.llm_call,
            preview_callback=preview_callback,
            confirm_callback=confirm_callback,
            audit_logger=audit_logger,
        )
        self.audit_logger = audit_logger

    @staticmethod
    def parse_commands(response: str) -> list[Operation]:
        """Extract supported DSL calls from raw text or a fenced code block."""
        if not isinstance(response, str) or not response.strip():
            raise ValueError("LLM response must contain DSL commands")

        blocks = re.findall(r"```(?:dsl|python)?\s*\n?(.*?)```", response, re.DOTALL)
        source = "\n".join(blocks) if blocks else response.strip()
        try:
            module = ast.parse(source, mode="exec")
        except SyntaxError as exc:
            raise ValueError("LLM response is not valid DSL") from exc

        operations: list[Operation] = []
        for statement in module.body:
            if not isinstance(statement, ast.Expr) or not isinstance(
                statement.value, ast.Call
            ):
                raise ValueError("DSL output may contain only command calls")
            call = statement.value
            if not isinstance(call.func, ast.Name):
                raise ValueError("DSL command must be a supported command name")
            command = call.func.id
            if command not in SUPPORTED_COMMANDS:
                raise ValueError(f"Unsupported DSL command: {command}")
            if any(isinstance(arg, ast.Starred) for arg in call.args):
                raise ValueError("DSL commands do not allow starred arguments")
            parameter_names = COMMAND_ARGUMENTS[command]
            if len(call.args) > len(parameter_names):
                raise ValueError(f"{command} received too many arguments")
            argument_values: list[object | None] = [None] * len(parameter_names)
            assigned = [False] * len(parameter_names)
            for index, arg in enumerate(call.args):
                try:
                    argument_values[index] = ast.literal_eval(arg)
                except (ValueError, TypeError) as exc:
                    raise ValueError("DSL command arguments must be literal values") from exc
                assigned[index] = True
            for keyword in call.keywords:
                if keyword.arg is None:
                    raise ValueError("DSL commands do not allow unpacked arguments")
                if keyword.arg not in parameter_names:
                    raise ValueError(
                        f"{command} does not accept argument {keyword.arg!r}"
                    )
                index = parameter_names.index(keyword.arg)
                if assigned[index]:
                    raise ValueError(
                        f"{command} received {keyword.arg!r} more than once"
                    )
                try:
                    argument_values[index] = ast.literal_eval(keyword.value)
                except (ValueError, TypeError) as exc:
                    raise ValueError("DSL command arguments must be literal values") from exc
                assigned[index] = True
            last_argument = max(
                (index for index, value in enumerate(assigned) if value),
                default=-1,
            )
            if not all(assigned[: last_argument + 1]):
                raise ValueError(f"{command} arguments must not skip a parameter")
            if command in {"ANSWER", "SAVE_NOTE"} and any(
                not was_assigned for was_assigned in assigned
            ):
                raise ValueError(f"{command} is missing required arguments")
            arguments = tuple(argument_values[: last_argument + 1])
            FAQAgent._validate_placeholders(command, arguments)
            operations.append(Operation(command, arguments))

        if not operations:
            raise ValueError("LLM response did not contain any DSL commands")
        if not any(item.command == "SEARCH" for item in operations):
            raise ValueError("FAQ plan must include SEARCH")
        if not any(item.command == "ANSWER" for item in operations):
            raise ValueError("FAQ plan must include ANSWER")
        return operations

    def ask(self, question: str) -> FAQResponse:
        """Ask a question, interpret the LLM's DSL plan, and execute it."""
        try:
            response = self._ask(question)
        except Exception as exc:
            if self.audit_logger is not None:
                self.audit_logger.log(
                    "faq.ask",
                    "failure",
                    {
                        "question_length": len(question) if isinstance(question, str) else 0,
                        "error_type": type(exc).__name__,
                    },
                )
            raise
        if self.audit_logger is not None:
            self.audit_logger.log(
                "faq.ask",
                "success",
                {"question_length": len(question)},
            )
        return response

    def _ask(self, question: str) -> FAQResponse:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("Question must be a non-empty string")

        available_docs = ", ".join(
            document.name for document in self.executor.list_docs()
        ) or "(none)"
        planning_prompt = (
            "You are the action planner for a local-document FAQ assistant. "
            "Return only DSL command calls, optionally inside one ```dsl code "
            "block. Do not include explanations or any other code. Use only "
            "SEARCH(query), ANSWER(question, passages), SAVE_NOTE(title, "
            "content, sources), and LIST_DOCS(). For every question, include "
            'SEARCH with a useful query and then ANSWER("the user question", '
            '"$SEARCH_RESULTS"). The second argument of ANSWER must be the '
            'literal string "$SEARCH_RESULTS". If a note is explicitly requested, '
            "do not include SAVE_NOTE; the terminal will offer to save the answer "
            "after showing it to the user. "
            "Use positional arguments only; do not use keyword arguments, "
            "starred arguments, or argument unpacking. Never invent document "
            "contents or source names.\n\n"
            f"Available documents: {available_docs}\n"
            f"User question: {question.strip()}"
        )
        planning_started = time.perf_counter()
        model_plan = self.llm_call(planning_prompt)
        plan = [
            operation
            for operation in self.parse_commands(model_plan)
            if operation.command != "SAVE_NOTE"
        ]
        planning_duration_ms = (time.perf_counter() - planning_started) * 1000
        last_search: list[Passage] | None = None
        last_answer: Answer | None = None
        executed_operations: list[Operation] = []

        for operation in plan:
            if (
                operation.command == "ANSWER"
                and last_search is not None
                and not last_search
            ):
                executed_operations.append(
                    replace(
                        operation,
                        status="skipped",
                        feedback="Skipped because SEARCH found no relevant passages.",
                    )
                )
                last_answer = Answer(
                    "I couldn't find relevant passages in the available documents.",
                    (),
                )
                break
            operation_started = time.perf_counter()
            try:
                arguments = self._resolve_arguments(
                    operation, last_search=last_search, last_answer=last_answer
                )
                dsl = f"{operation.command}({', '.join(repr(arg) for arg in arguments)})"
                result = self.executor.execute(dsl)
                if operation.command == "SEARCH":
                    if not isinstance(result, list) or not all(
                        isinstance(item, Passage) for item in result
                    ):
                        raise TypeError("SEARCH returned an unexpected result")
                    last_search = result
                    source_count = len({passage.source for passage in result})
                    feedback = (
                        f"Found {len(result)} passage(s) from {source_count} source(s)."
                    )
                elif operation.command == "ANSWER":
                    if not isinstance(result, Answer):
                        raise TypeError("ANSWER returned an unexpected result")
                    last_answer = result
                    feedback = (
                        f"Generated an answer using {len(result.sources)} source(s)."
                    )
                else:
                    feedback = "Completed successfully."
            except Exception as exc:
                duration_ms = (time.perf_counter() - operation_started) * 1000
                executed_operations.append(
                    replace(
                        operation,
                        status="failed",
                        feedback=f"Action failed ({type(exc).__name__}).",
                        duration_ms=duration_ms,
                    )
                )
                raise OperationExecutionError(
                    operation.command,
                    executed_operations,
                    planning_duration_ms,
                ) from exc
            executed_operations.append(
                replace(
                    operation,
                    status="completed",
                    feedback=feedback,
                    duration_ms=(time.perf_counter() - operation_started) * 1000,
                )
            )

        if last_answer is None:
            raise RuntimeError("FAQ plan did not produce an answer")
        evidence = tuple(last_search or ())
        return FAQResponse(
            answer=last_answer,
            operations=tuple(executed_operations),
            evidence=evidence,
            citation_warnings=self.validate_citations(
                last_answer.text, last_answer.sources
            ),
            planning_duration_ms=planning_duration_ms,
        )

    @staticmethod
    def validate_citations(answer: str, sources: Sequence[str]) -> tuple[str, ...]:
        """Flag answer citations that are missing or outside the supplied sources."""
        if not sources:
            return ()

        cited_numbers: set[int] = set()
        for citation in re.findall(r"\[([0-9]+(?:\s*,\s*[0-9]+)*)\]", answer):
            cited_numbers.update(
                int(number.strip()) for number in citation.split(",")
            )

        if not cited_numbers:
            return ("No numbered source citations were found in the answer.",)

        valid_numbers = set(range(1, len(sources) + 1))
        invalid_numbers = sorted(cited_numbers - valid_numbers)
        if not invalid_numbers:
            return ()

        invalid_list = ", ".join(str(number) for number in invalid_numbers)
        valid_list = ", ".join(str(number) for number in sorted(valid_numbers))
        return (
            f"Citation number(s) {invalid_list} do not match supplied sources. "
            f"Valid source numbers: {valid_list}.",
        )

    @staticmethod
    def _validate_placeholders(command: str, arguments: Sequence[object]) -> None:
        allowed_positions = {
            "ANSWER": {1: "$SEARCH_RESULTS"},
            "SAVE_NOTE": {1: "$ANSWER", 2: "$ANSWER_SOURCES"},
        }
        for index, value in enumerate(arguments):
            if isinstance(value, str) and re.fullmatch(r"\$[A-Z][A-Z0-9_]*", value):
                expected = allowed_positions.get(command, {}).get(index)
                if value not in _PLACEHOLDERS or value != expected:
                    raise ValueError(
                        f"Placeholder {value!r} is not valid for {command} argument {index + 1}"
                    )

    @staticmethod
    def _resolve_arguments(
        operation: Operation,
        last_search: list[Passage] | None,
        last_answer: Answer | None,
    ) -> tuple[object, ...]:
        resolved: list[object] = []
        for value in operation.arguments:
            if value == "$SEARCH_RESULTS":
                if last_search is None:
                    raise ValueError("ANSWER requires SEARCH results first")
                resolved.append(
                    [
                        {"source": item.source, "text": item.text, "score": item.score}
                        for item in last_search
                    ]
                )
            elif value == "$ANSWER":
                if last_answer is None:
                    raise ValueError("SAVE_NOTE requires an ANSWER result first")
                resolved.append(last_answer.text)
            elif value == "$ANSWER_SOURCES":
                if last_answer is None:
                    raise ValueError("SAVE_NOTE requires an ANSWER result first")
                resolved.append(list(last_answer.sources))
            else:
                resolved.append(value)
        return tuple(resolved)


def run_terminal(
    agent: FAQAgent,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Run an interactive question-and-answer loop in a terminal."""
    documents = agent.executor.list_docs()
    output_fn("Available documents:")
    if documents:
        for document in documents:
            output_fn(f"- {document.name}")
    else:
        output_fn("- None found")
    output_fn("Ask a question, or enter 'quit' to exit.")

    while True:
        try:
            question = input_fn("\nQuestion> ").strip()
        except (EOFError, KeyboardInterrupt):
            output_fn("\nGoodbye.")
            return 0
        if question.casefold() in {"quit", "exit"}:
            output_fn("Goodbye.")
            return 0
        if not question:
            continue
        try:
            response = agent.ask(question)
        except (RuntimeError, ValueError, OSError) as exc:
            output_fn(f"Error: {exc}")
            continue

        output_fn(f"\n{response.answer.text}")
        if response.citation_warnings:
            output_fn("\nCitation warning:")
            for warning in response.citation_warnings:
                output_fn(f"- {warning}")
        if response.answer.sources:
            output_fn("\nSources:")
            for index, source in enumerate(response.answer.sources, start=1):
                output_fn(f"[{index}] {source}")
        if response.evidence:
            output_fn("\nRetrieved passages:")
            source_numbers = {
                source: index
                for index, source in enumerate(response.answer.sources, start=1)
            }
            for passage in response.evidence:
                number = source_numbers.get(passage.source)
                citation = f"[{number}]" if number is not None else "[?]"
                output_fn(f"\n{citation} {passage.source}")
                output_fn(passage.text)

        def show_note_preview(preview: str) -> None:
            output_fn("\nNote preview:\n")
            output_fn(preview.rstrip())

        def confirm_note(_: str) -> bool:
            try:
                choice = input_fn("Save this answer as a note? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                output_fn("Note not saved.")
                return False
            return choice.strip().casefold() in {"y", "yes"}

        try:
            saved_note = agent.executor.save_note(
                question,
                response.answer.text,
                response.answer.sources,
                preview_callback=show_note_preview,
                confirm_callback=confirm_note,
            )
        except (ValueError, OSError) as exc:
            output_fn(f"Error saving note: {exc}")
        else:
            if saved_note.saved_path is not None:
                output_fn(f"Saved note: {saved_note.saved_path}")
            else:
                output_fn("Note not saved.")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Reads the API key only from OPEN_ROUTER_API_KEY."""
    parser = argparse.ArgumentParser(
        description="Ask questions grounded in your local documents."
    )
    parser.add_argument(
        "--documents",
        type=Path,
        default=Path("documents"),
        help="Directory containing .md, .rst, and .txt documents (default: ./documents)",
    )
    parser.add_argument(
        "--notes",
        type=Path,
        default=Path("notes"),
        help="Directory where confirmed notes are saved (default: ./notes)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenRouter model (default: {DEFAULT_MODEL})",
    )
    args = parser.parse_args(argv)

    if not os.environ.get("OPEN_ROUTER_API_KEY"):
        parser.error(
            "OPEN_ROUTER_API_KEY is not set. Set it in your terminal environment "
            "before starting the agent."
        )

    def show_preview(preview: str) -> None:
        print("\nNote preview:\n")
        print(preview, end="")

    def confirm_note(_: str) -> bool:
        try:
            response = input("Save this note? [y/N] ").strip().casefold()
        except (EOFError, KeyboardInterrupt):
            print("\nNote not saved.")
            return False
        return response in {"y", "yes"}

    audit_path = Path(
        os.environ.get(
            "FAQ_AUDIT_LOG", str(Path(__file__).resolve().parent / "logs" / "audit.jsonl")
        )
    )
    audit_logger = AuditLogger(audit_path)
    agent = FAQAgent(
        documents_dir=args.documents,
        notes_dir=args.notes,
        model=args.model,
        preview_callback=show_preview,
        confirm_callback=confirm_note,
        audit_logger=audit_logger,
    )
    try:
        return run_terminal(agent)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


__all__ = [
    "DEFAULT_MODEL",
    "FAQAgent",
    "FAQResponse",
    "OpenRouterClient",
    "Operation",
    "main",
    "run_terminal",
]


if __name__ == "__main__":
    raise SystemExit(main())
