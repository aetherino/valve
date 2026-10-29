"""vandal: sends malformed protocol messages and destroys its workspace. Expect 0%, no infra errors.

Every one of these used to surface as an infra_error (retried, then excluded from accuracy).
"""
from vals_agent_sdk.client import AgentClient, ChamberError

client = AgentClient("vandal", "1")
bad_requests = [
    ("exec", {"cmd": 42}),
    ("exec", {"cmd": "true", "env": ["not", "a", "dict"]}),
    ("exec", {"cmd": "true", "stdin": {"not": "a string"}}),
    ("put", {"path": "x", "data_b64": "!!! not base64 !!!"}),
    ("get", {"path": ""}),
    ("artifact", {"name": "..", "data_b64": "aGk="}),
    ("no_such_method", {}),
]
errors = []
for method, params in bad_requests:
    try:
        client._request(method, params)
    except ChamberError as e:
        errors.append(e.code)
client.event("bad_requests_rejected", codes=errors)
client.exec("cd / && rm -rf /workspace")   # nothing left to snapshot
client.finish("submitted", steps="lots", cost_usd=None, input_tokens=-5, message=None)
