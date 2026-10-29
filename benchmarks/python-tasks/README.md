# Python Tasks Benchmark

A simple benchmark for testing Python coding capabilities of AI agents using pytest.

## Overview

This benchmark contains 3 simple Python programming tasks:
- Write an addition function (2+2)
- Implement Fibonacci sequence
- Get nth element from a list

Tasks are verified by running pytest tests against the agent's solution.

## Dataset Structure

Each task in `dataset.json` contains:
- `id`: Unique identifier (e.g., "py-001")
- `prompt`: Natural language description of the function to implement
- `docker_image`: Docker image to use ("python:3.11-slim")
- `solution_file`: Name of file where agent should write solution (default: "solution.py")
- `test_code`: Pytest test code that will be used to verify the solution


## Dependencies

This benchmark requires pytest for test execution. The `requirements.txt` file contains:
```
pytest>=7.0.0
```

Dependencies are automatically installed during setup.

## Usage

### Setup Task

Prepare the workspace with test files and install dependencies:

```bash
python prepare.py py-001 --workspace /path/to/workspace
```

This:
1. Installs pytest from requirements.txt
2. Creates `test_solution.py` - pytest tests for the task
3. Creates `solution.py` - empty stub file where the agent writes code

### Evaluate Task

Run pytest to verify the solution:

```bash
python grade.py py-001 --workspace /path/to/workspace
```

Returns JSON result:
```json
{
  "task_id": "py-001",
  "passed": true,
  "score": 1.0,
  "details": {
    "stdout": "...pytest output...",
    "stderr": "",
    "return_code": 0
  }
}
```

Exit code:
- Always exits with `0` (check the JSON output to determine pass/fail)

## Docker Environment

All tasks are designed to run in Docker containers using the `python:3.11-slim` image specified in each task's `docker_image` field.

## Evaluation

- Uses `pytest` to run tests (installed via requirements.txt during setup)
- 30-second timeout for test execution
- Score is 1.0 if all tests pass, 0.0 otherwise
- Test output is included in result details

## Example Workflow

```bash
# 1. Setup workspace
python prepare.py py-002 --workspace /tmp/workspace

# 2. Agent writes solution in solution.py:
#    def fibonacci(n):
#        if n <= 1:
#            return n
#        return fibonacci(n-1) + fibonacci(n-2)

# 3. Evaluate with pytest
python grade.py py-002 --workspace /tmp/workspace
```

## Scoring

Each task is scored as pass/fail (1.0 or 0.0) based on pytest results. The overall benchmark score is the percentage of tasks that pass all tests.
