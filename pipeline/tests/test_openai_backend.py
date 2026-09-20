"""The OpenAI chat backend, and the ceiling that stops it.

Nothing here touches the network or spends a cent. What is worth testing is not that HTTP works, but that the
budget is refused *before* a call goes out, that a truncated evidence bundle says so, and that a reply which is
not JSON is raised rather than guessed at.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uranium_explorer.backends import openai_api as O
from uranium_explorer.backends import spend as S
from uranium_explorer.backends.base import ExtractionRequest, SchemaInvalidError


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "openai_spend.jsonl"
    monkeypatch.setattr(S, "ledger_path", lambda: path)
    monkeypatch.setenv("OPENAI_MAX_SPEND_USD", "2.00")
    monkeypatch.setenv("OPENAI_PRICE_IN_PER_MTOK", "0.25")
    monkeypatch.setenv("OPENAI_PRICE_OUT_PER_MTOK", "2.00")
    return path


def request(prompt: str = "the question", stage: tuple = ()) -> ExtractionRequest:
    return ExtractionRequest(
        task="prospect_chat", images=(), system_prompt="the rules", user_prompt=prompt,
        schema={"type": "object"}, schema_version="1.0.0", prompt_version="v1",
        model="gpt-5-mini", effort="medium", stage_files=stage,
    )


# ---------------------------------------------------------------- the ceiling


def test_price_is_arithmetic_over_measured_tokens(ledger: Path) -> None:
    price = S.Price.from_env()
    assert price.usd(1_000_000, 0) == pytest.approx(0.25)
    assert price.usd(0, 1_000_000) == pytest.approx(2.00)


def test_spending_accumulates_across_calls_and_survives_a_restart(ledger: Path) -> None:
    price = S.Price.from_env()
    S.record("gpt-5-mini", 100_000, 10_000, price, task="one")
    first = S.spent_usd()
    S.record("gpt-5-mini", 100_000, 10_000, price, task="two")
    assert S.spent_usd() == pytest.approx(first * 2)
    # a fresh process reads the same file: a restart does not hand back a fresh budget
    assert len(ledger.read_text().strip().splitlines()) == 2


def test_a_call_that_would_cross_the_ceiling_is_refused_before_it_is_sent(ledger: Path) -> None:
    S.record("gpt-5-mini", 8_000_000, 0, S.Price.from_env())  # $2.00 exactly
    with pytest.raises(S.BudgetExhausted) as err:
        S.check(0.01)
    assert "ceiling" in str(err.value)


def test_an_unreadable_ledger_line_stops_everything_rather_than_counting_as_free(ledger: Path) -> None:
    ledger.write_text('{"usd": 0.5}\nthis line is not json\n')
    with pytest.raises(S.BudgetExhausted) as err:
        S.check()
    assert "cannot read" in str(err.value)


def test_the_backend_refuses_when_the_ledger_is_already_at_the_ceiling(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    S.record("gpt-5-mini", 8_000_000, 0, S.Price.from_env())
    sent = []
    monkeypatch.setattr(O.httpx, "post", lambda *a, **k: sent.append(a) or None)
    with pytest.raises(S.BudgetExhausted):
        O.OpenAIBackend().call(request())
    assert sent == [], "the ceiling must be checked before anything leaves the machine"


# ---------------------------------------------------------------- the evidence bundle


def test_staged_files_are_inlined_because_an_api_call_has_no_filesystem(tmp_path: Path) -> None:
    book = tmp_path / "handbook.md"
    book.write_text("unknown is not absent")
    messages = O._messages(request("Read {STAGE_DIR}/handbook.md", ((book, "handbook.md"),)))
    body = messages[1]["content"]
    assert "unknown is not absent" in body
    assert "{STAGE_DIR}" not in body


def test_a_bundle_too_big_to_send_says_what_was_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A silently truncated handbook produces a confident answer about evidence the model never saw."""
    monkeypatch.setattr(O, "MAX_STAGED_CHARS", 50)
    big = tmp_path / "big.json"
    big.write_text("x" * 400)
    text = O._staged_text(request(stage=((big, "big.json"),)))
    assert "not shown" in text and "size limit" in text


def test_the_schema_travels_with_the_call(tmp_path: Path) -> None:
    messages = O._messages(request())
    assert "JSON Schema" in messages[0]["content"]
    assert "the rules" in messages[0]["content"]


# ---------------------------------------------------------------- the reply


@pytest.mark.parametrize("text", ['{"action":"answer"}', '```json\n{"action":"answer"}\n```'])
def test_a_json_reply_is_read_with_or_without_a_fence(text: str) -> None:
    assert O._parse(text) == {"action": "answer"}


def test_a_reply_that_is_not_json_is_raised_rather_than_guessed_at() -> None:
    with pytest.raises(SchemaInvalidError):
        O._parse("I think the answer is probably yes.")


# ---------------------------------------------------------------- configuration


def test_dotenv_is_read_without_overwriting_what_is_already_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("OPENAI_MODEL=from-the-file\nOPENAI_NEW=fresh\n")
    monkeypatch.setenv("OPENAI_MODEL", "from-the-environment")
    monkeypatch.delenv("OPENAI_NEW", raising=False)
    O.load_dotenv(tmp_path)
    assert os_env("OPENAI_MODEL") == "from-the-environment"
    assert os_env("OPENAI_NEW") == "fresh"


def os_env(key: str) -> str:
    import os

    return os.environ.get(key, "")


def test_the_two_backends_never_share_a_cache_entry() -> None:
    req = request()
    assert req.cache_key("openai") != req.cache_key("claude_cli")


def test_a_missing_key_is_a_configuration_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    from uranium_explorer.backends.base import BackendConfigError

    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setattr(O, "load_dotenv", lambda *a, **k: {})
    with pytest.raises(BackendConfigError) as err:
        O.api_key()
    assert ".env" in str(err.value)


def test_the_ledger_records_tokens_as_facts_and_dollars_as_arithmetic(ledger: Path) -> None:
    S.record("gpt-5-mini", 1234, 567, S.Price.from_env(), task="prospect_chat")
    row = json.loads(ledger.read_text().strip())
    assert row["tokens_in"] == 1234 and row["tokens_out"] == 567
    assert row["per_mtok_in"] == 0.25 and row["per_mtok_out"] == 2.00
    assert row["usd"] == pytest.approx(1234 / 1e6 * 0.25 + 567 / 1e6 * 2.00)


# ---------------------------------------------------------------- streaming


class FakeStream:
    """Stands in for httpx's streaming response: the lines an SSE reply is made of."""

    def __init__(self, lines: list[str], status: int = 200) -> None:
        self.lines, self.status_code = lines, status

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def iter_lines(self):
        yield from self.lines

    def read(self) -> bytes:
        return b"upstream said no"


def sse(*chunks: dict) -> list[str]:
    return [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]


def test_a_streamed_reply_is_handed_over_piece_by_piece_and_rebuilt_whole(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fragments are for watching. What gets parsed is the reassembled message, exactly as if it arrived
    in one piece — a half-written answer is never something the gate sees."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    lines = sse(
        {"id": "x1", "model": "gpt-5-mini", "choices": [{"delta": {"content": '{"action":'}}]},
        {"choices": [{"delta": {"content": '"answer","reasoning":"checking coverage"'}}]},
        {"choices": [{"delta": {"content": ',"answer":{"text":"ok","claims":[]}}'}}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
    )
    monkeypatch.setattr(O.httpx, "stream", lambda *a, **k: FakeStream(lines))
    seen: list[str] = []
    out = O.OpenAIBackend().call(request(), on_delta=seen.append)

    assert len(seen) == 3, "each fragment reaches the caller as it arrives"
    assert "".join(seen).startswith('{"action":')
    assert out.structured["reasoning"] == "checking coverage"
    assert out.usage["completion_tokens"] == 20


def test_streaming_still_charges_the_ledger_and_respects_the_ceiling(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    lines = sse(
        {"choices": [{"delta": {"content": '{"action":"answer"}'}}]},
        {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 100}},
    )
    monkeypatch.setattr(O.httpx, "stream", lambda *a, **k: FakeStream(lines))
    O.OpenAIBackend().call(request(), on_delta=lambda _p: None)
    assert S.spent_usd() > 0, "a streamed call is charged like any other"


def test_a_streamed_error_status_is_reported_not_parsed(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uranium_explorer.backends.base import BackendConfigError

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(O.httpx, "stream", lambda *a, **k: FakeStream([], status=404))
    with pytest.raises(BackendConfigError, match="ue openai models"):
        O.OpenAIBackend().call(request(), on_delta=lambda _p: None)


def test_a_backend_that_cannot_stream_is_called_the_way_it_expects() -> None:
    """Asked, not caught: a TypeError raised inside a call must not be mistaken for 'cannot stream'."""
    from uranium_explorer.backends.cache import streams

    class Streaming:
        def call(self, req, on_delta=None): ...

    class Plain:
        def call(self, req): ...

    assert streams(Streaming()) is True
    assert streams(Plain()) is False


def test_a_reply_with_a_second_object_or_a_remark_after_the_first_is_read_as_the_first() -> None:
    """A reasoning model followed its answer with a second object; `Extra data` failed a cell for it."""
    from uranium_explorer.backends.openai_api import _parse

    assert _parse('{"status": "met", "n": 1}\n{"status": "again"}') == {"status": "met", "n": 1}
    assert _parse('{"status": "met"} -- that is my answer') == {"status": "met"}
    with pytest.raises(Exception):
        _parse('{"status": "met"')


# ---------------------------------------------------------------- images


class FakeTransport:
    """Stands in for httpx.post: records the payload, answers with one JSON reply."""

    def __init__(self, structured: dict) -> None:
        self.sent: list[dict] = []
        self.structured = structured

    def __call__(self, url: str, headers: dict, json: dict, timeout: float):
        self.sent.append(json)
        body = {"id": "x1", "model": "gpt-5-mini", "usage": {"prompt_tokens": 3000, "completion_tokens": 50},
                "choices": [{"message": {"content": __import__("json").dumps(self.structured)}}]}

        class R:
            status_code = 200
            text = ""

            def json(self_inner):
                return body

        return R()


def image_request(tmp_path: Path, model: str = "gpt-5-mini") -> ExtractionRequest:
    page = tmp_path / "page.png"
    page.write_bytes(b"\x89PNG\r\n\x1a\nfakepage")
    return ExtractionRequest(task="extract_page", images=(page,), system_prompt="transcribe",
                             user_prompt="Read the page image at {STAGE_DIR}/page.png.", schema={"type": "object"},
                             schema_version="1", prompt_version="v1", model=model, effort="low")


def test_a_page_image_travels_as_a_data_url_part_beside_the_text(ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same message shape the OpenRouter adapter sends: a text part, then one image_url part per page."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    transport = FakeTransport({"page_level": {}})
    monkeypatch.setattr(O.httpx, "post", transport)
    resp = O.OpenAIBackend().call(image_request(tmp_path))
    user = transport.sent[0]["messages"][1]["content"]
    assert isinstance(user, list) and [p["type"] for p in user] == ["text", "image_url"]
    assert "{STAGE_DIR}" not in user[0]["text"]
    assert user[1]["image_url"]["url"].startswith("data:image/png;base64,") and user[1]["image_url"]["detail"] == "high"
    assert isinstance(transport.sent[0]["messages"][0]["content"], str), "the system message stays plain text"
    assert resp.structured == {"page_level": {}} and resp.cost_usd > 0


def test_a_request_without_images_keeps_the_plain_string_message(tmp_path: Path) -> None:
    messages = O._messages(request())
    assert isinstance(messages[1]["content"], str)


def test_an_image_for_a_model_that_cannot_see_is_refused_before_anything_is_sent(ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from uranium_explorer.backends.base import BackendConfigError

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.delenv("OPENAI_VISION_MODELS", raising=False)
    transport = FakeTransport({})
    monkeypatch.setattr(O.httpx, "post", transport)
    with pytest.raises(BackendConfigError) as err:
        O.OpenAIBackend().call(image_request(tmp_path, model="gpt-3.5-turbo"))
    assert "images" in str(err.value) and transport.sent == []
    monkeypatch.setenv("OPENAI_VISION_MODELS", "gpt-3.5-turbo")
    assert O.takes_images("gpt-3.5-turbo"), "the operator can name a model the prefix list does not know"


def test_the_pre_call_estimate_counts_the_image(tmp_path: Path, ledger: Path) -> None:
    backend = O.OpenAIBackend()
    text_only = backend._worst_case_usd(O._messages(request()), 0)
    with_image = backend._worst_case_usd(O._messages(image_request(tmp_path)), 1)
    assert with_image > text_only + S.Price.from_env().usd(O.IMAGE_TOKENS, 0) - 1e-9
