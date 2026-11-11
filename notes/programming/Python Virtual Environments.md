# Python Virtual Environments

A virtual environment is an isolated folder holding its own Python interpreter and packages, so each project keeps its own dependency versions. Create one with `python -m venv .venv` and activate it with `.venv\Scripts\activate` on Windows. Pin versions in requirements.txt and freeze a lock file for reproducible installs. Never commit the .venv folder; see [[Git Branching Basics]] for what belongs in a repository. Related: [[Unit Testing with Pytest]].
