"""Discord notifier Lambda.

Subscribes to the ``AgenticsAlertsTopic`` SNS topic and posts a formatted
message to a Discord DM channel using the bot token stored in Secrets Manager.

Flow per SNS record:
    1. Parse the SNS message (JSON published by the State Machine terminal state).
    2. Look up the run's SPEC item in DynamoDB for richer context (project name).
    3. Build the Discord payload (``build_discord_embed`` — user-decided format).
    4. POST to Discord's channel messages endpoint with ``Authorization: Bot``.

Exceptions posting to Discord are logged but NOT re-raised — re-raising would
trigger SNS to retry the whole batch, which could spam the user's DM.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from functools import lru_cache
from typing import Any

import boto3
from aws_lambda_powertools import Logger, Tracer


logger = Logger()
tracer = Tracer()


@lru_cache(maxsize=1)
def _load_bot_token() -> str:
    secret_arn = os.environ["DISCORD_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(response["SecretString"])
    return secret["DISCORD_BOT_TOKEN"]


def _fetch_run_metadata(run_id: str, runs_table: str) -> dict:
    """Pull the SPEC item from DynamoDB for additional context (project name, etc).

    Returns {} on any failure — metadata is nice-to-have, not required.
    """
    if not run_id:
        return {}
    client = boto3.client("dynamodb")
    try:
        response = client.get_item(
            TableName=runs_table,
            Key={"run_id": {"S": run_id}, "item_type": {"S": "SPEC"}},
        )
        return _deserialize_ddb_item(response.get("Item", {}))
    except Exception:
        logger.exception("run_metadata_fetch_failed", extra={"run_id": run_id})
        return {}


def _deserialize_ddb_item(item: dict) -> dict:
    """Minimal DynamoDB JSON -> flat dict.

    Only handles S/N/BOOL types (enough for SPEC items in this pipeline).
    """
    out: dict = {}
    for key, typed in item.items():
        if "S" in typed:
            out[key] = typed["S"]
        elif "N" in typed:
            out[key] = float(typed["N"])
        elif "BOOL" in typed:
            out[key] = bool(typed["BOOL"])
    return out


def build_discord_embed(msg: dict, run: dict) -> dict:
    """Build the Discord message payload for a pipeline terminal event.

    TODO (Isaac — ~10 lines): customize this to your taste.

    Signals available in ``msg`` (published by State Machine Notify states):
        - run_id:          str
        - status:          'DONE' | 'EXHAUSTED' | 'UNRESOLVABLE' | 'FAILED'
        - iteration:       int (may be absent for early-fail states)
        - scores:          {review: {score, passed, ...}, security: {...}, scalability: {...}}
        - manifest_s3_uri: str  (may be absent)

    Signals available in ``run`` (DynamoDB SPEC item, best-effort):
        - project_name, requirements, language, complexity, ...

    Design decisions that are YOURS to make (with recommendations):
      - Color:  green 0x22c55e (DONE) / amber 0xeab308 (EXHAUSTED) /
                red 0xef4444 (UNRESOLVABLE, FAILED)
      - Fields: validator scores as 3 inline fields? iteration? S3 manifest link?
      - Title:  include project_name if present, plus an emoji per status.
      - Content (line above the embed): do you want it to @-ping yourself?

    Return shape must match Discord's API:
        {
          "content": "optional plain text",
          "embeds": [{
            "title": "...",
            "description": "...",
            "color": 0x22c55e,
            "fields": [{"name": "Review", "value": "91/100 ok", "inline": True}]
          }]
        }
    """
    status = msg.get("status", "UNKNOWN")
    run_id = msg.get("run_id", "unknown")
    iteration = msg.get("iteration")
    scores = msg.get("scores") or {}
    manifest = msg.get("manifest_s3_uri")
    project = run.get("project_name") or "Agentics Run"

    # Warm palette: gold=DONE, amber=EXHAUSTED, terracotta=UNRESOLVABLE, beige=FAILED
    color_by_status = {
        "DONE":         0xD4AF37,  # dorado
        "EXHAUSTED":    0xF59E0B,  # ambar
        "UNRESOLVABLE": 0xC1440E,  # terracota (dead-end arquitectural)
        "FAILED":       0xD4B88C,  # beige
    }
    emoji_by_status = {"DONE": "✨", "EXHAUSTED": "⚠️", "UNRESOLVABLE": "🚫", "FAILED": "💥"}

    fields: list[dict] = []
    for name in ("review", "security", "scalability"):
        v = scores.get(name)
        if isinstance(v, dict) and v.get("score") is not None:
            ok = "✅" if v.get("passed") else "❌"
            fields.append({"name": name.title(), "value": f"{v['score']}/100 {ok}", "inline": True})
    if iteration is not None:
        fields.append({"name": "Iteration", "value": str(iteration), "inline": True})
    if manifest:
        fields.append({"name": "Manifest", "value": f"`{manifest}`", "inline": False})

    return {
        "embeds": [{
            "title": f"{emoji_by_status.get(status, '•')} {project} — {status}",
            "description": f"Run ID: `{run_id}`",
            "color": color_by_status.get(status, 0xD4B88C),
            "fields": fields,
        }]
    }


def _post_to_discord(channel_id: str, payload: dict, bot_token: str) -> None:
    """POST the message to Discord. Raises ``urllib.error.HTTPError`` on non-2xx."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "AgenticsCodeTeam-Notifier/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        logger.info("discord_post_ok", extra={"status": response.status})


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
def lambda_handler(event: dict, context: Any) -> dict:
    channel_id = os.environ["DISCORD_DM_CHANNEL_ID"]
    runs_table = os.environ["RUNS_TABLE"]
    bot_token = _load_bot_token()

    processed = 0
    for record in event.get("Records", []):
        try:
            msg = json.loads(record["Sns"]["Message"])
        except (KeyError, json.JSONDecodeError):
            logger.exception("sns_record_parse_failed")
            continue

        # Filter: only pipeline terminal events have `run_id` + `status`.
        # Everything else (CloudWatch alarms, etc.) publishes to this same topic
        # but should NOT reach Discord — that is handled by other channels.
        if not ("run_id" in msg and "status" in msg):
            logger.info("non_pipeline_event_ignored", extra={"keys": list(msg.keys())[:10]})
            continue

        run = _fetch_run_metadata(msg.get("run_id", ""), runs_table)
        payload = build_discord_embed(msg, run)

        try:
            _post_to_discord(channel_id, payload, bot_token)
            processed += 1
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            logger.exception(
                "discord_post_http_error",
                extra={"status": exc.code, "body": body, "run_id": msg.get("run_id")},
            )
        except Exception:
            logger.exception("discord_post_failed", extra={"run_id": msg.get("run_id")})

    return {"processed": processed}
