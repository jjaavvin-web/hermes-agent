from pathlib import Path
import json
import re
import urllib.request

base = "http://127.0.0.1:9119"
html = urllib.request.urlopen(base + "/", timeout=15).read().decode("utf-8")
match = re.search(r'window\.__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"', html)
print("token_found", bool(match))
if not match:
    raise SystemExit(2)
req = urllib.request.Request(
    base + "/api/dashboard/cost",
    headers={"X-Hermes-Session-Token": match.group(1)},
)
with urllib.request.urlopen(req, timeout=5) as resp:
    data = json.loads(resp.read().decode("utf-8"))
Path("scratchpad/live-current-cost.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
print("status", resp.status)
print("live_has_dailySeries", "dailySeries" in data)
print("live_has_cacheLatency7d", "cacheLatency7d" in data)
print("live_legacy", {k: (k in data) for k in ["today", "last7d", "meteredLeak", "meteredLeakCount", "meteredLeakCostUsd"]})
