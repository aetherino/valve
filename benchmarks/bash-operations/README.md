# Bash Operations Benchmark

A simple benchmark for testing basic bash command execution capabilities of AI agents.

## Overview

This benchmark contains 5 tasks that test an agent's ability to execute basic bash operations like:
- Creating files with specific content
- Creating directories and files
- Counting lines in files
- Listing files with specific extensions
- Generating sequences

## Dataset Structure

Each task in `dataset.json` contains:
- `task_id`: Unique identifier (e.g., "bash-001")
- `problem_statement`: Natural language description of what the agent should do
- `docker_image`: Docker image to use for the task environment ("alpine:latest")
- `verification`: Criteria for evaluating the solution
- `difficulty`: Task difficulty level
- `setup` (optional): Files to create in the workspace before running the agent

## Verification Types

### `file_content`
Checks exact match of file content (after stripping whitespace).

```json
{
  "type": "file_content",
  "file": "hello.txt",
  "expected_content": "Hello World"
}
```

### `file_exists`
Checks if all specified files exist.

```json
{
  "type": "file_exists",
  "files": [
    "test_dir/file1.txt",
    "test_dir/file2.txt",
    "test_dir/file3.txt"
  ]
}
```

### `file_content_contains`
Checks if file contains all expected lines (substring match).

```json
{
  "type": "file_content_contains",
  "file": "txt_files.txt",
  "expected_lines": [
    "file1.txt",
    "file2.txt",
    "document.txt"
  ]
}
```

## Usage

### Setup Task

Prepare the workspace for a specific task:

```bash
python setup.py --workspace-dir /path/to/workspace --task bash-001
```

### Evaluate Task

Evaluate the workspace after the agent has completed the task:

```bash
python evaluate.py --workspace-dir /path/to/workspace --task bash-001
```

Returns JSON result:
```json
{
  "task_id": "bash-001",
  "passed": true,
  "score": 1.0,
  "details": {
    "expected": "Hello World",
    "actual": "Hello World"
  }
}
```

Exit codes:
- `0`: Task passed
- `1`: Task failed

## Docker Environment

All tasks are designed to run in Docker containers using the `alpine:latest` image specified in each task's `docker_image` field.

## Scoring

Each task is scored as pass/fail (1.0 or 0.0). The overall benchmark score is the percentage of tasks passed.

## Example Workflow

```bash
# 1. Setup workspace
python setup.py --workspace-dir /tmp/workspace --task bash-003

# 2. Agent runs and solves the task (creates count.txt)

# 3. Evaluate results
python evaluate.py --workspace-dir /tmp/workspace --task bash-003
```
