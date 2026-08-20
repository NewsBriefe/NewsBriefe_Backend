import json
from unittest.mock import MagicMock

import pytest

from app.services.groq_summarizer import GroqSummarizationService


@pytest.fixture
def service():
    instance = GroqSummarizationService.__new__(GroqSummarizationService)
    instance._client = MagicMock()
    instance._model = "openai/gpt-oss-20b"
    return instance


def test_parse_valid_summary(service):
    raw = json.dumps({
        "sentence_1": "The event happened.",
        "sentence_2": "It affects the public.",
        "sentence_3": "Officials will respond next.",
    })

    summary = service._parse_summary(raw)

    assert summary.full == (
        "The event happened. It affects the public. Officials will respond next."
    )


@pytest.mark.parametrize("raw", [
    "not json",
    '{"sentence_1":"Only one sentence."}',
    '{"sentence_1":"One.","sentence_2":"","sentence_3":"Three."}',
    '{"sentence_1":"One.","sentence_2":"Two.","sentence_3":3}',
])
def test_parse_rejects_invalid_or_incomplete_summary(service, raw):
    with pytest.raises(ValueError):
        service._parse_summary(raw)


def test_invoke_requests_json_and_reads_message_content(service):
    choice = MagicMock()
    choice.message.content = (
        '{"sentence_1":"One.","sentence_2":"Two.","sentence_3":"Three."}'
    )
    service._client.chat.completions.create.return_value.choices = [choice]

    result = service._invoke("prompt", json_mode=True)

    assert result == choice.message.content
    kwargs = service._client.chat.completions.create.call_args.kwargs
    assert kwargs["messages"] == [{"role": "user", "content": "prompt"}]
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["reasoning_effort"] == "low"


def test_invoke_rejects_empty_completion(service):
    service._client.chat.completions.create.return_value.choices = []

    with pytest.raises(ValueError, match="empty completion"):
        service._invoke("prompt")


def test_plain_text_request_does_not_enable_json_mode(service):
    choice = MagicMock()
    choice.message.content = "world"
    service._client.chat.completions.create.return_value.choices = [choice]

    assert service._invoke("categorize") == "world"
    kwargs = service._client.chat.completions.create.call_args.kwargs
    assert "response_format" not in kwargs
