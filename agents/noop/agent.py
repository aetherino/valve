"""noop: does nothing. Expect 0% — proves graders don't pass an untouched workspace."""
from valve_agent_sdk.client import AgentClient

client = AgentClient("noop", "1")
client.finish("submitted", steps=0)
