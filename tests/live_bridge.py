"""Check the deployed signed capstone bridge using two independent browsers."""
import http.cookiejar
import json
import sys
import urllib.request

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3002"


def browser():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def request(client, path, body=None, headers=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with client.open(req, timeout=30) as response:
        return json.load(response)


first, second = browser(), browser()
request(first, "/api/state")
request(second, "/api/state")
state = request(first, "/api/killchain/state")
examples = request(first, "/api/killchain/examples")
request(first, "/api/killchain/reviews", {"product": state["products"][0]["sku"], "rating": 5, "text": "Good quality.", "hidden": examples["procedures"][0]["text"]})
poisoned = request(first, "/api/killchain/state")
assert len(poisoned["memory"]["connector"]) == 1
assert len(poisoned["memory"]["agent"]) == 1
isolated = request(second, "/api/killchain/state", headers={"X-Nine-Session": "forged", "X-Nine-Signature": "forged"})
assert isolated["memory"]["connector"] == []
assert isolated["memory"]["agent"] == []
assert isolated["overall"] == "baseline"
request(first, "/api/killchain/cleanup/hard_reset", {})
assert request(first, "/api/state")["summary"]["score"] == 0
print(json.dumps({"status": "passed", "signed_bridge": True, "separate_learners": True, "client_identity_headers_ignored": True, "ctf_progress_independent": True}))
