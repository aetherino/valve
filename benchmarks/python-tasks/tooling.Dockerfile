# Tooling image for python-tasks: runs prepare.py and grade.py (build context = this folder).
FROM python:3.11-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
