"""Evaluation script for bash-operations benchmark tasks."""

import argparse
import json
import sys
from pathlib import Path


def evaluate_task(workspace: Path, task_id: str, dataset_path: Path) -> dict:
    """Evaluate workspace for a specific task.

    Args:
        workspace: Path to workspace directory
        task_id: Task identifier
        dataset_path: Path to dataset.json

    Returns:
        dict: Evaluation result with task_id, passed, score, and details
    """
    # Load dataset
    with open(dataset_path) as f:
        tasks = {task["task_id"]: task for task in json.load(f)}

    if task_id not in tasks:
        return {
            "task_id": task_id,
            "passed": False,
            "score": 0.0,
            "details": {"error": f"Task {task_id} not found in dataset"},
        }

    task = tasks[task_id]
    verification = task["verification"]
    verification_type = verification["type"]

    passed = False
    details = {}

    try:
        if verification_type == "file_content":
            # Check exact file content match
            file_path = workspace / verification["file"]
            if file_path.exists():
                actual_content = file_path.read_text().strip()
                expected_content = verification["expected_content"].strip()
                passed = actual_content == expected_content
                details["expected"] = expected_content
                details["actual"] = actual_content
            else:
                details["error"] = f"File {verification['file']} not found"

        elif verification_type == "file_exists":
            # Check if all files exist
            files = verification["files"]
            existing_files = []
            missing_files = []

            for file in files:
                file_path = workspace / file
                if file_path.exists():
                    existing_files.append(file)
                else:
                    missing_files.append(file)

            passed = len(missing_files) == 0
            details["existing_files"] = existing_files
            details["missing_files"] = missing_files

        elif verification_type == "file_content_contains":
            # Check if file contains expected lines
            file_path = workspace / verification["file"]
            if file_path.exists():
                actual_content = file_path.read_text()
                expected_lines = verification["expected_lines"]

                found_lines = []
                missing_lines = []

                for line in expected_lines:
                    if line in actual_content:
                        found_lines.append(line)
                    else:
                        missing_lines.append(line)

                passed = len(missing_lines) == 0
                details["found_lines"] = found_lines
                details["missing_lines"] = missing_lines
            else:
                details["error"] = f"File {verification['file']} not found"

        else:
            details["error"] = f"Unknown verification type: {verification_type}"

    except Exception as e:
        details["error"] = str(e)
        passed = False

    return {
        "task_id": task_id,
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "details": details,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate workspace for bash-operations task"
    )
    parser.add_argument(
        "--workspace-dir", required=True, help="Path to workspace directory"
    )
    parser.add_argument("--task", required=True, help="Task identifier")

    args = parser.parse_args()

    workspace = Path(args.workspace_dir)
    dataset_path = Path(__file__).parent / "dataset.json"

    result = evaluate_task(workspace, args.task, dataset_path)

    # Print result as JSON
    print(json.dumps(result, indent=2))

    # Exit with appropriate code
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
