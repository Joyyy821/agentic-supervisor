from setuptools import find_packages, setup


setup(
    name="agentic-supervisor",
    version="0.1.0",
    description="Persistent, general-purpose supervisor for Codex development sessions",
    python_requires=">=3.10",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    entry_points={"console_scripts": ["agentic-supervisor=agentic_supervisor.cli:main"]},
)
