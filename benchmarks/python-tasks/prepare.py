"""Setup script for python-tasks benchmark tasks."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def setup_task(workspace: Path, task_id: str, dataset_path: Path) -> None:
    """Set up workspace for a specific Python task.

    Args:
        workspace: Path to workspace directory
        task_id: Task identifier
        dataset_path: Path to dataset.json
    """
    # Install requirements (pytest)
    requirements_path = Path(__file__).parent / "requirements.txt"
    if requirements_path.exists():
        print("Installing requirements...")
        subprocess.run(
            ["pip", "install", "-q", "-r", str(requirements_path)],
            check=True,
        )
        print("✓ Requirements installed")

    # Load dataset
    with open(dataset_path) as f:
        tasks = {task["id"]: task for task in json.load(f)}

    if task_id not in tasks:
        print(f"Error: Task {task_id} not found in dataset", file=sys.stderr)
        sys.exit(1)

    task = tasks[task_id]

    # Create workspace directory
    workspace.mkdir(parents=True, exist_ok=True)

    # Create test file
    test_file_path = workspace / "test_solution.py"
    test_file_path.write_text(task["test_code"])
    print("Created test_solution.py")

    # Create empty solution file stub
    solution_file = task.get("solution_file", "solution.py")
    solution_path = workspace / solution_file
    if not solution_path.exists():
        solution_path.write_text("# Write your solution here\n")
        print(f"Created {solution_file}")

    print(f"✓ Setup complete for task {task_id}")


def main():
    parser = argparse.ArgumentParser(
        description="Setup workspace for python-tasks task"
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

    setup_task(workspace, args.task_id, config_path)


if __name__ == "__main__":
    main()
