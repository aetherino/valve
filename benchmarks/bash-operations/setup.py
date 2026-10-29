"""Setup script for bash-operations benchmark tasks."""

import argparse
import json
import sys
from pathlib import Path


def setup_task(workspace: Path, task_id: str, dataset_path: Path) -> None:
    """Set up workspace for a specific task.

    Args:
        workspace: Path to workspace directory
        task_id: Task identifier
        dataset_path: Path to dataset.json
    """
    # Load dataset
    with open(dataset_path) as f:
        tasks = {task["task_id"]: task for task in json.load(f)}

    if task_id not in tasks:
        print(f"Error: Task {task_id} not found in dataset", file=sys.stderr)
        sys.exit(1)

    task = tasks[task_id]

    # Create workspace directory
    workspace.mkdir(parents=True, exist_ok=True)

    # Set up any initial files specified in task
    if "setup" in task and "files" in task["setup"]:
        for filename, content in task["setup"]["files"].items():
            file_path = workspace / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)
            print(f"Created {filename}")

    print(f"✓ Setup complete for task {task_id}")


def main():
    parser = argparse.ArgumentParser(
        description="Setup workspace for bash-operations task"
    )
    parser.add_argument(
        "--workspace-dir", required=True, help="Path to workspace directory"
    )
    parser.add_argument("--task", required=True, help="Task identifier")

    args = parser.parse_args()

    workspace = Path(args.workspace_dir)
    dataset_path = Path(__file__).parent / "dataset.json"

    setup_task(workspace, args.task, dataset_path)


if __name__ == "__main__":
    main()
