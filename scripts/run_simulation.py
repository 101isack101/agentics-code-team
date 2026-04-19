"""Dispatcher for the three canned simulations under tests/simulations/.

Two execution modes:

* ``--mode local`` — runs each agent Lambda locally via
  ``sam local invoke`` in the canonical Spec → CodeGen → (Review|Security|
  Scalability) → Iterator sequence. This does **not** execute the Step
  Functions state machine — it only chains invocations so you can smoke-test
  individual handlers against the canned event.

* ``--mode aws`` — starts a real Step Functions execution via
  ``StartExecution`` and polls ``DescribeExecution`` every 5s, printing
  status transitions until the execution finishes. Requires valid AWS
  credentials and a deployed stack whose state-machine ARN can be resolved
  from the CloudFormation stack outputs.

Usage::

    python scripts/run_simulation.py --sim 01 --mode local
    python scripts/run_simulation.py --sim 02 --mode aws --stage dev
    python scripts/run_simulation.py --sim 03 --mode local --dry-run

Dependency: only ``boto3`` (already in requirements-dev.txt).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SIM_DIR = ROOT / "tests" / "simulations"

LOCAL_SEQUENCE = [
    ("SpecAgentFunction", "spec"),
    ("CodeGenAgentFunction", "codegen"),
    ("ReviewAgentFunction", "review"),
    ("SecurityAgentFunction", "security"),
    ("ScalabilityAgentFunction", "scalability"),
    ("IteratorAgentFunction", "iterator"),
]


def _resolve_sim(sim_id: str) -> Path:
    matches = sorted(SIM_DIR.glob(f"{sim_id}_*.json"))
    if not matches:
        raise SystemExit(f"no simulation found for id '{sim_id}' under {SIM_DIR}")
    return matches[0]


def _run_local(sim_path: Path, dry_run: bool) -> int:
    print(f"[local] simulation: {sim_path.name}")
    print(f"[local] sequence: {' -> '.join(fn for fn, _ in LOCAL_SEQUENCE)}")
    if dry_run:
        print("[local] --dry-run: skipping actual sam local invoke calls")
        return 0

    for function_name, label in LOCAL_SEQUENCE:
        print(f"[local] >>> sam local invoke {function_name} ({label})")
        cmd = [
            "sam",
            "local",
            "invoke",
            function_name,
            "--event",
            str(sim_path),
        ]
        proc = subprocess.run(cmd, cwd=str(ROOT))
        if proc.returncode != 0:
            print(f"[local] {function_name} failed (exit={proc.returncode}); aborting.")
            return proc.returncode
    return 0


def _stack_name(stage: str) -> str:
    return f"agentics-code-team-{stage}"


def _resolve_state_machine_arn(stage: str) -> str:
    import boto3

    cfn = boto3.client("cloudformation")
    stack_name = _stack_name(stage)
    resp = cfn.describe_stacks(StackName=stack_name)
    outputs = resp["Stacks"][0].get("Outputs", [])
    for o in outputs:
        if o["OutputKey"] == "MainStateMachineArn":
            return o["OutputValue"]
    raise SystemExit(f"StateMachineArn output not found on stack {stack_name}")


def _run_aws(sim_path: Path, stage: str, dry_run: bool) -> int:
    import boto3

    arn = _resolve_state_machine_arn(stage)
    event = json.loads(sim_path.read_text(encoding="utf-8"))
    # Append timestamp so re-runs don't collide on DynamoDB run_id key.
    event["run_id"] = f"{event['run_id']}-{int(time.time())}"
    payload = json.dumps(event)
    print(f"[aws] stage={stage} stateMachineArn={arn}")
    if dry_run:
        print("[aws] --dry-run: skipping StartExecution")
        return 0

    sfn = boto3.client("stepfunctions")
    started = sfn.start_execution(stateMachineArn=arn, input=payload)
    exec_arn = started["executionArn"]
    print(f"[aws] executionArn={exec_arn}")

    last_status: str | None = None
    while True:
        desc = sfn.describe_execution(executionArn=exec_arn)
        status = desc["status"]
        if status != last_status:
            print(f"[aws] status -> {status}")
            last_status = status
        if status != "RUNNING":
            print("[aws] final output:")
            print(desc.get("output") or desc.get("cause") or "<no output>")
            return 0 if status == "SUCCEEDED" else 1
        time.sleep(5)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run a canned simulation end-to-end.")
    p.add_argument("--sim", required=True, help="Simulation id (e.g. 01, 02, 03).")
    p.add_argument("--mode", choices=("local", "aws"), required=True)
    p.add_argument("--stage", default="dev", help="CloudFormation stage for --mode aws.")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    sim_path = _resolve_sim(args.sim)
    # Surface the event so the operator sees exactly what will be sent.
    print(json.dumps(json.loads(sim_path.read_text(encoding="utf-8")), indent=2))

    if args.mode == "local":
        return _run_local(sim_path, args.dry_run)
    return _run_aws(sim_path, args.stage, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
