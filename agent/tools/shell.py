import subprocess
from langchain_core.tools import tool


@tool
def shell(command: str) -> str:
    """Run a shell command and return its output. Use for any CLI operation."""
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout
    if result.stderr:
        output += f"\n[stderr]\n{result.stderr}"
    if result.returncode != 0:
        output += f"\n[exit code: {result.returncode}]"
    return output or "(no output)"
