"""oracle: replays a known-good shell script per task. Expect 100% — proves the pipeline end to end."""
from pathlib import Path

from vals_agent_sdk.client import AgentClient

client = AgentClient("oracle", "1")
script = Path(__file__).parent / "solutions" / f"{client.task.get('id')}.sh"
if not script.exists():
    client.finish("gave_up", message=f"no solution for {client.task.get('id')}")
else:
    r = client.exec(script.read_text())
    client.event("oracle_ran", returncode=r["returncode"])
    client.finish("submitted" if r["returncode"] == 0 else "agent_error", steps=1,
                  message=r["output"][-500:])
