"""Real CLI verification only. No fixtures, mocks, or synthetic accounts."""

import argparse
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime

from mpwatch.cli import private_path


def run_step(name, args, timeout=60):
    try:
        process = subprocess.run(
            [sys.executable, "-m", "mpwatch", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"step": name, "status": "failed", "error": "process_timeout"}
    try:
        result = json.loads(process.stdout)
    except ValueError:
        return {"step": name, "status": "failed", "error": "invalid_cli_output"}
    return {
        "step": name,
        "status": "passed" if process.returncode == 0 else "failed",
        "exit_code": process.returncode,
        "result": result,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Verify against a real, explicitly selected account"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--article-url")
    target.add_argument("--book-id")
    args = parser.parse_args()
    steps = []
    commands = [
        ("authentication", ["auth", "status"], 60),
        (
            "subscription",
            ["subscribe", "--article-url", args.article_url]
            if args.article_url
            else ["subscribe", "--book-id", args.book_id],
            90,
        ),
        ("collection", ["refresh"], 360),
    ]
    for name, command, timeout in commands:
        if steps and steps[-1]["status"] != "passed":
            steps.append({"step": name, "status": "not_run", "reason": "prior_step_failed"})
        else:
            step = run_step(name, command, timeout)
            if name == "collection" and step.get("exit_code") in {0, 2}:
                articles = step["result"].get("articles", [])
                selected = steps[1]["result"]["account"]["book_id"]
                ready = [
                    a
                    for a in articles
                    if a.get("book_id") == selected
                    and a.get("content_status") == "ready"
                    and (a.get("body") or {}).get("text")
                ]
                if not ready:
                    step.update(status="inconclusive", reason="no_target_body_observed_in_this_run")
                else:
                    step["target_bodies_observed"] = len(ready)
                    if step["exit_code"] == 2:
                        step.update(
                            status="partial", reason="body_observed_but_coverage_incomplete"
                        )
            steps.append(step)
    if steps[-1]["status"] in {"passed", "partial"}:
        steps.append(
            run_step("saved_report", ["report", "--run-id", steps[-1]["result"]["run_id"]])
        )
        if steps[-1]["status"] == "passed" and steps[-1]["result"] != steps[-2]["result"]:
            steps[-1].update(status="failed", error="saved_report_mismatch")
    else:
        steps.append({"step": "saved_report", "status": "not_run", "reason": "prior_step_failed"})
    status = "passed" if all(step["status"] == "passed" for step in steps) else "not_passed"
    receipt = {
        "schema_version": 1,
        "mode": "live",
        "status": status,
        "target": {"article_url": args.article_url, "book_id": args.book_id},
        "executed_at": datetime.now(UTC).isoformat(),
        "steps": steps,
    }
    directory = private_path("MPWATCH_DATA_DIR") / "verification"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (uuid.uuid4().hex + ".json")
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "mode": "live",
                "status": status,
                "receipt": str(path),
                "steps": [{"step": s["step"], "status": s["status"]} for s in steps],
            }
        )
    )
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
