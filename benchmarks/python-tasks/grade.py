"""Evaluation script for python-tasks benchmark tasks."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def evaluate_task(workspace: Path, task_id: str, dataset_path: Path) -> dict:
    """Evaluate workspace for a specific Python task.

    Args:
        workspace: Path to workspace directory
        task_id: Task identifier
        dataset_path: Path to dataset.json

    Returns:
        dict: Evaluation result with task_id, passed, score, and details
    """
    # Load dataset
    with open(dataset_path) as f:
        tasks = {task["id"]: task for task in json.load(f)}

    if task_id not in tasks:
        return {
            "task_id": task_id,
            "passed": False,
            "score": 0.0,
            "details": {"error": f"Task {task_id} not found in dataset"},
        }

    task = tasks[task_id]
    solution_file = task.get("solution_file", "solution.py")

    # Check if solution file exists
    solution_path = workspace / solution_file
    if not solution_path.exists():
        return {
            "task_id": task_id,
            "passed": False,
            "score": 0.0,
            "details": {"error": f"Solution file {solution_file} not found"},
        }

    # Check if test file exists
    test_file_path = workspace / "test_solution.py"
    if not test_file_path.exists():
        return {
            "task_id": task_id,
            "passed": False,
            "score": 0.0,
            "details": {"error": "Test file test_solution.py not found"},
        }

    # Run pytest
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "test_solution.py", "-v"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
        )

        passed = result.returncode == 0

        details = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.returncode,
        }

        return {
            "task_id": task_id,
            "passed": passed,
            "score": 1.0 if passed else 0.0,
            "details": details,
        }

    except subprocess.TimeoutExpired:
        return {
            "task_id": task_id,
            "passed": False,
            "score": 0.0,
            "details": {"error": "Test execution timed out (30s)"},
        }
    except Exception as e:
        return {
            "task_id": task_id,
            "passed": False,
            "score": 0.0,
            "details": {"error": str(e)},
        }


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate workspace for python-tasks task"
    )
    parser.add_argument("task_id", help="Task identifier")
    parser.add_argument(
        "--workspace", required=True, help="Path to workspace directory"
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to dataset.json (default: same directory as this script)",
    )

    args = parser.parse_args()

    workspace = Path(args.workspace)
    config_path = (
        Path(args.config) if args.config else Path(__file__).parent / "dataset.json"
    )

    result = evaluate_task(workspace, args.task_id, config_path)

    # Print result as JSON
    print(json.dumps(result, indent=2))

    # Always exit 0 (check JSON for pass/fail)
    sys.exit(0)


if __name__ == "__main__":
    main()
