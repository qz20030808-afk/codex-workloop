"""Run a cross-day replay using entirely fictional inventory data."""
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from pathlib import Path

from workloop import Workloop, parse_time

DAY_ONE = parse_time("2026-11-22T16:00:00+08:00")
MIDNIGHT = parse_time("2026-11-23T00:00:00+08:00")
DAY_TWO = parse_time("2026-11-23T09:00:00+08:00")
DAY_TWO_CLOSED = parse_time("2026-11-24T00:00:00+08:00")
SAMPLE = "sku,stock,reorder_point\nPAPER,3,5\nINK,12,4\n"


def seed(root: Path) -> dict:
    with Workloop(root) as loop:
        loop.start("Produce a reorder list from a synthetic inventory fixture.", {
            "input": "Validate the CSV schema and row count.",
            "result": "Generate and verify the list of SKUs below the reorder point.",
        }, DAY_ONE)
        artifacts = root / "artifacts"
        artifacts.mkdir(exist_ok=True)
        (artifacts / "inventory.csv").write_text(SAMPLE, encoding="utf-8")
        rows = list(csv.DictReader(SAMPLE.splitlines()))
        assert len(rows) == 2 and set(rows[0]) == {"sku", "stock", "reorder_point"}
        loop.accept("input", "inventory.csv", "Checked the schema and both fixture rows.", DAY_ONE)
        try:
            loop.complete(DAY_ONE)
        except ValueError as error:
            if "Unmet acceptance requirements: result" not in str(error):
                raise
        else:
            raise AssertionError("Premature completion was not rejected.")
        return {"phase": "day_one", "completion_blocked": True, "pending": ["result"]}


def scheduled_report(root: Path) -> dict:
    with Workloop(root) as loop:
        reports = loop.report(MIDNIGHT)
    assert [report["key"] for report in reports] == [
        "daily-2026-11-22", "weekly-2026-11-22"
    ]
    return {"phase": "midnight_report", "logical_at": MIDNIGHT.isoformat(), "reports": reports}


def finish(root: Path) -> dict:
    with Workloop(root) as loop:
        handoff = loop.resume(DAY_TWO)
        assert handoff["pending"] == ["result"]
        with (root / "artifacts" / "inventory.csv").open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        flagged = [row["sku"] for row in rows if int(row["stock"]) < int(row["reorder_point"])]
        assert flagged == ["PAPER"], flagged
        (root / "artifacts" / "reorder.json").write_text(json.dumps(flagged) + "\n", encoding="utf-8")
        loop.accept("result", "reorder.json", "Checked that only PAPER is below its reorder point.", DAY_TWO)
        receipt = loop.complete(DAY_TWO)
        reports = loop.report(DAY_TWO_CLOSED)
        return {"phase": "day_two", "handoff": handoff, "receipt": receipt, "reports": reports}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("phase", choices=["replay", "seed", "report", "finish"], default="replay", nargs="?")
    cli.add_argument("--state-dir", type=Path, help="Directory for event state and generated artifacts.")
    args = cli.parse_args()
    if args.phase == "replay":
        root = args.state_dir or Path(tempfile.mkdtemp(prefix="workloop-"))
        result = {"state_dir": str(root), "steps": [seed(root), scheduled_report(root), finish(root)]}
    else:
        if args.state_dir is None:
            cli.error("--state-dir is required for phased execution")
        run = {"seed": seed, "report": scheduled_report, "finish": finish}[args.phase]
        result = run(args.state_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
