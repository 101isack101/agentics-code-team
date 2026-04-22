"""Unit tests for the Discord notifier Lambda.

Covers:
    - SNS record parsing (good + malformed)
    - DynamoDB metadata fetch (success + failure paths)
    - DDB item deserialization for S/N/BOOL types
    - Secrets Manager token loading (via lru_cache)
    - Discord POST: success and HTTP error must NOT raise out of the handler
      (re-raising would trigger SNS retries and DM spam).

Does NOT exercise real Discord or real boto3 — both are mocked.

Note: ``lambdas/discord_notifier/app.py`` shares the module name ``app`` with
``lambdas/spec_agent/app.py``. We load it via importlib under a unique name
(``discord_notifier_app``) to avoid collision with the spec_agent conftest path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


_DISCORD_APP_PATH = (
    Path(__file__).resolve().parents[2]
    / "lambdas"
    / "discord_notifier"
    / "app.py"
)


def _load_discord_app():
    """Load the discord_notifier app module under a unique name."""
    spec = importlib.util.spec_from_file_location(
        "discord_notifier_app", _DISCORD_APP_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["discord_notifier_app"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app(monkeypatch):
    """Fresh module with required env vars and cleared lru_cache."""
    monkeypatch.setenv("DISCORD_DM_CHANNEL_ID", "1485389933807013932")
    monkeypatch.setenv(
        "DISCORD_SECRET_ARN",
        "arn:aws:secretsmanager:us-east-1:000000000000:secret:discord-test",
    )
    monkeypatch.setenv("RUNS_TABLE", "agentics-runs-test")
    module = _load_discord_app()
    # Reset the cached token in case another test populated it.
    module._load_bot_token.cache_clear()
    return module


def _sns_event(message: dict) -> dict:
    """Build a minimal SNS Lambda event with a single record."""
    return {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {
                    "Message": json.dumps(message),
                    "MessageId": "00000000-0000-0000-0000-000000000000",
                    "TopicArn": "arn:aws:sns:us-east-1:000000000000:AgenticsAlertsTopic",
                },
            }
        ]
    }


# ---------------------------------------------------------------------------
# Helpers: _deserialize_ddb_item
# ---------------------------------------------------------------------------


def test_deserialize_ddb_item_handles_string_number_bool(app):
    item = {
        "project_name": {"S": "DemoApp"},
        "complexity_score": {"N": "42.5"},
        "is_prod": {"BOOL": True},
    }
    out = app._deserialize_ddb_item(item)
    assert out == {
        "project_name": "DemoApp",
        "complexity_score": 42.5,
        "is_prod": True,
    }


def test_deserialize_ddb_item_empty_input_returns_empty_dict(app):
    assert app._deserialize_ddb_item({}) == {}


def test_deserialize_ddb_item_skips_unsupported_types(app):
    """L (list), M (map), NULL are silently ignored — we only need S/N/BOOL."""
    item = {
        "tags": {"L": [{"S": "a"}, {"S": "b"}]},
        "project_name": {"S": "Kept"},
    }
    out = app._deserialize_ddb_item(item)
    assert out == {"project_name": "Kept"}


# ---------------------------------------------------------------------------
# _fetch_run_metadata
# ---------------------------------------------------------------------------


def test_fetch_run_metadata_returns_empty_when_run_id_missing(app):
    assert app._fetch_run_metadata("", "table") == {}


def test_fetch_run_metadata_success_returns_deserialized_item(app):
    ddb = MagicMock()
    ddb.get_item.return_value = {
        "Item": {
            "project_name": {"S": "DemoApp"},
            "complexity": {"S": "simple"},
        }
    }
    with patch.object(app.boto3, "client", return_value=ddb):
        out = app._fetch_run_metadata("run-123", "agentics-runs-test")
    assert out == {"project_name": "DemoApp", "complexity": "simple"}
    ddb.get_item.assert_called_once_with(
        TableName="agentics-runs-test",
        Key={"run_id": {"S": "run-123"}, "item_type": {"S": "SPEC"}},
    )


def test_fetch_run_metadata_returns_empty_on_ddb_failure(app):
    """DDB errors must NEVER bubble up — metadata is nice-to-have only."""
    ddb = MagicMock()
    ddb.get_item.side_effect = RuntimeError("DDB unavailable")
    with patch.object(app.boto3, "client", return_value=ddb):
        out = app._fetch_run_metadata("run-123", "agentics-runs-test")
    assert out == {}


def test_fetch_run_metadata_returns_empty_when_item_absent(app):
    ddb = MagicMock()
    ddb.get_item.return_value = {}  # No "Item" key = not found in DDB
    with patch.object(app.boto3, "client", return_value=ddb):
        out = app._fetch_run_metadata("run-missing", "agentics-runs-test")
    assert out == {}


# ---------------------------------------------------------------------------
# _load_bot_token (lru_cache)
# ---------------------------------------------------------------------------


def test_load_bot_token_reads_from_secrets_manager(app):
    sm = MagicMock()
    sm.get_secret_value.return_value = {
        "SecretString": json.dumps({"DISCORD_BOT_TOKEN": "sekret-abc"})
    }
    with patch.object(app.boto3, "client", return_value=sm):
        token = app._load_bot_token()
    assert token == "sekret-abc"


def test_load_bot_token_is_cached(app):
    """lru_cache means the second call does NOT hit Secrets Manager again."""
    sm = MagicMock()
    sm.get_secret_value.return_value = {
        "SecretString": json.dumps({"DISCORD_BOT_TOKEN": "sekret-abc"})
    }
    with patch.object(app.boto3, "client", return_value=sm):
        app._load_bot_token()
        app._load_bot_token()
        app._load_bot_token()
    assert sm.get_secret_value.call_count == 1


# ---------------------------------------------------------------------------
# build_discord_embed (placeholder behavior — user's TODO)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected_color",
    [
        ("DONE",         0xD4AF37),  # gold
        ("EXHAUSTED",    0xF59E0B),  # amber
        ("UNRESOLVABLE", 0xC1440E),  # terracotta
        ("FAILED",       0xD4B88C),  # beige
    ],
)
def test_build_discord_embed_assigns_correct_color_per_status(app, status, expected_color):
    msg = {"run_id": "run-xyz", "status": status}
    payload = app.build_discord_embed(msg, {})
    assert payload["embeds"][0]["color"] == expected_color


def test_build_discord_embed_includes_scores_as_inline_fields(app):
    msg = {
        "run_id": "run-abc",
        "status": "DONE",
        "iteration": 2,
        "scores": {
            "review":      {"score": 91, "passed": True},
            "security":    {"score": 82, "passed": True},
            "scalability": {"score": 92, "passed": True},
        },
        "manifest_s3_uri": "s3://bucket/manifest.json",
    }
    payload = app.build_discord_embed(msg, {"project_name": "DemoApp"})
    embed = payload["embeds"][0]
    field_names = [f["name"] for f in embed["fields"]]

    assert "Review" in field_names
    assert "Security" in field_names
    assert "Scalability" in field_names
    assert "Iteration" in field_names
    assert "Manifest" in field_names
    assert "DemoApp" in embed["title"]
    assert "DONE" in embed["title"]


def test_build_discord_embed_handles_missing_fields_gracefully(app):
    """Must not KeyError on minimal SNS messages (e.g. early-failure states)."""
    payload = app.build_discord_embed({}, {})
    assert isinstance(payload, dict)
    assert "embeds" in payload
    # No scores, no iteration, no manifest → fields list must still be empty (not error).
    assert payload["embeds"][0]["fields"] == []


# ---------------------------------------------------------------------------
# _post_to_discord
# ---------------------------------------------------------------------------


def test_post_to_discord_sends_correct_request(app):
    """Verify URL, auth header, and JSON-serialized body."""
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data
        captured["method"] = request.get_method()
        return FakeResponse()

    with patch.object(app.urllib.request, "urlopen", side_effect=fake_urlopen):
        app._post_to_discord("channel-123", {"content": "hi"}, "bot-token")

    assert captured["url"] == "https://discord.com/api/v10/channels/channel-123/messages"
    assert captured["method"] == "POST"
    # urllib lowercases header keys.
    auth_header = {k.lower(): v for k, v in captured["headers"].items()}
    assert auth_header["authorization"] == "Bot bot-token"
    assert auth_header["content-type"] == "application/json"
    assert json.loads(captured["body"].decode("utf-8")) == {"content": "hi"}


# ---------------------------------------------------------------------------
# lambda_handler — the integration of the above
# ---------------------------------------------------------------------------


def test_handler_processes_all_records_and_posts_to_discord(app, lambda_context):
    """Happy path: 1 SNS record → 1 Discord POST → processed=1."""
    event = _sns_event(
        {
            "run_id": "run-abc",
            "status": "DONE",
            "iteration": 1,
            "scores": {
                "review": {"score": 91, "passed": True},
                "security": {"score": 82, "passed": True},
                "scalability": {"score": 92, "passed": True},
            },
            "manifest_s3_uri": "s3://bucket/artifacts/run-abc/manifest.json",
        }
    )

    with patch.object(app, "_load_bot_token", return_value="bot-token"), patch.object(
        app, "_fetch_run_metadata", return_value={"project_name": "DemoApp"}
    ), patch.object(app, "_post_to_discord") as post_spy:
        result = app.lambda_handler(event, lambda_context)

    assert result == {"processed": 1}
    post_spy.assert_called_once()
    channel_id, payload, bot_token = post_spy.call_args.args
    assert channel_id == "1485389933807013932"
    assert bot_token == "bot-token"
    assert isinstance(payload, dict)


def test_handler_ignores_cloudwatch_alarm_events(app, lambda_context):
    """CloudWatch alarms share the SNS topic — they must NOT reach Discord.

    Regression: after initial deploy we saw `• Agentics Run — UNKNOWN` DMs
    because alarm payloads (with AlarmName/NewStateValue fields but no run_id)
    were being processed by build_discord_embed's defaults.
    """
    alarm_event = {
        "Records": [
            {
                "Sns": {
                    "Message": json.dumps({
                        "AlarmName": "StateMachineFailedAlarm",
                        "NewStateValue": "ALARM",
                        "Reason": "1 datapoint > 0",
                    })
                }
            }
        ]
    }
    with patch.object(app, "_load_bot_token", return_value="bot-token"), patch.object(
        app, "_post_to_discord"
    ) as post_spy:
        result = app.lambda_handler(alarm_event, lambda_context)

    assert result == {"processed": 0}
    post_spy.assert_not_called()


def test_handler_ignores_messages_with_run_id_but_no_status(app, lambda_context):
    """Both fields required — partial messages are not valid pipeline events."""
    event = _sns_event({"run_id": "run-abc"})  # status missing
    with patch.object(app, "_load_bot_token", return_value="bot-token"), patch.object(
        app, "_post_to_discord"
    ) as post_spy:
        result = app.lambda_handler(event, lambda_context)

    assert result == {"processed": 0}
    post_spy.assert_not_called()


def test_handler_skips_malformed_sns_records(app, lambda_context):
    """Garbage SNS message is logged but does NOT crash the batch."""
    event = {
        "Records": [
            {"Sns": {"Message": "not-valid-json{"}},
            {"Sns": {"Message": json.dumps({"run_id": "r1", "status": "DONE"})}},
        ]
    }
    with patch.object(app, "_load_bot_token", return_value="bot-token"), patch.object(
        app, "_fetch_run_metadata", return_value={}
    ), patch.object(app, "_post_to_discord") as post_spy:
        result = app.lambda_handler(event, lambda_context)

    # Only the valid record was processed.
    assert result == {"processed": 1}
    assert post_spy.call_count == 1


def test_handler_does_not_re_raise_on_discord_http_error(app, lambda_context):
    """Discord 5xx must NOT propagate — would trigger SNS retry → DM spam."""
    import urllib.error

    event = _sns_event({"run_id": "r1", "status": "DONE"})

    http_err = urllib.error.HTTPError(
        url="https://discord.com/api/v10/channels/x/messages",
        code=503,
        msg="Service Unavailable",
        hdrs=None,
        fp=None,
    )

    with patch.object(app, "_load_bot_token", return_value="bot-token"), patch.object(
        app, "_fetch_run_metadata", return_value={}
    ), patch.object(app, "_post_to_discord", side_effect=http_err):
        # The point: no exception escapes.
        result = app.lambda_handler(event, lambda_context)

    # processed counter did NOT increment for the failed record.
    assert result == {"processed": 0}


def test_handler_does_not_re_raise_on_unexpected_exception(app, lambda_context):
    """Any unexpected error (network timeout, TypeError, etc.) must be swallowed."""
    event = _sns_event({"run_id": "r1", "status": "FAILED"})

    with patch.object(app, "_load_bot_token", return_value="bot-token"), patch.object(
        app, "_fetch_run_metadata", return_value={}
    ), patch.object(app, "_post_to_discord", side_effect=RuntimeError("boom")):
        result = app.lambda_handler(event, lambda_context)

    assert result == {"processed": 0}


@pytest.mark.parametrize(
    "status", ["DONE", "EXHAUSTED", "UNRESOLVABLE", "FAILED"]
)
def test_handler_works_for_each_terminal_status(app, lambda_context, status):
    """Smoke-check all 4 terminal states the state machine publishes."""
    event = _sns_event({"run_id": f"run-{status.lower()}", "status": status})

    with patch.object(app, "_load_bot_token", return_value="bot-token"), patch.object(
        app, "_fetch_run_metadata", return_value={}
    ), patch.object(app, "_post_to_discord") as post_spy:
        result = app.lambda_handler(event, lambda_context)

    assert result == {"processed": 1}
    assert post_spy.call_count == 1
