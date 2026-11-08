"""Drive one REAL research run against a running stack (spends Anthropic + Tavily credits).

    APPFACTORY_API_KEY=<a key from API_KEYS> python scripts/live_research.py \
        --brief "Find underserved consumer app opportunities ..." --region "United States" --category fitness

Submits the run, polls until it finishes, then prints the ranked opportunities. It NEVER approves
anything: approval is a separate, deliberate human call to POST /api/v1/opportunities/{id}/decision.
Standard library only.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

DEFAULT_BRIEF = (
    "Find underserved consumer mobile app opportunities that a small team could build with Flutter and "
    "monetize with Google AdMob. Prefer problems with frequent repeat use and clear user complaints about "
    "existing apps."
)


DEFAULT_GAME_BRIEF = (
    "Find 2D mobile game concepts that one developer could build in Godot and monetize with rewarded ads. "
    "Each concept must have a distinctive core mechanic, a 10-15 second clip that makes a stranger want to play, "
    "and a plausible way to be discovered without paid acquisition."
)


def call(base: str, key: str, method: str, path: str, body: dict | None = None):
    request = urllib.request.Request(
        f"{base}{path}", method=method, data=json.dumps(body).encode() if body else None,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--domain", choices=("apps", "games"), default="apps")
    parser.add_argument("--brief", default=None, help="defaults to a brief suited to --domain")
    parser.add_argument("--region", action="append", default=[])
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--max", type=int, default=5, dest="max_opportunities")
    parser.add_argument("--timeout-minutes", type=int, default=45)
    args = parser.parse_args()

    key = os.environ.get("APPFACTORY_API_KEY")
    if not key:
        print("Set APPFACTORY_API_KEY to one of the keys in API_KEYS (value after 'name:').", file=sys.stderr)
        return 2

    brief = args.brief or (DEFAULT_GAME_BRIEF if args.domain == "games" else DEFAULT_BRIEF)
    created = call(args.url, key, "POST", "/api/v1/research-runs", {
        "brief": brief, "regions": args.region or ["Global"], "categories": args.category,
        "max_opportunities": args.max_opportunities, "domain": args.domain,
    })
    run_id = created["run_id"]
    print(f"run {run_id} queued")

    deadline = time.time() + args.timeout_minutes * 60
    status = "queued"
    while time.time() < deadline:
        run = call(args.url, key, "GET", f"/api/v1/research-runs/{run_id}")
        if run["status"] != status:
            status = run["status"]
            print(f"  status: {status}")
        if status in ("completed", "failed"):
            break
        time.sleep(15)
    else:
        print("timed out waiting; the run keeps going server-side. Poll GET /api/v1/research-runs/" + run_id)
        return 3

    if status == "failed":
        print("FAILED:", run["error_message"])
        return 1

    print(f"\nsummary: {run['summary']}\nsources recorded: {run['source_count']}\n")
    for opp_id in run["opportunity_ids"]:
        o = call(args.url, key, "GET", f"/api/v1/opportunities/{opp_id}")
        print(f"{o['total_score']:6.2f}  conf={o['confidence_label']:<6}  {o['decision_status']:<17}  {o['name']}  [{o['id']}]")
        if o["domain"] == "games":
            d = o["details"]
            print(f"          genre: {d.get('genre')} | loop: {d.get('core_loop')}\n          twist: {d.get('twist')}")
        for w in o["warnings"][:3]:
            print(f"          ! {w}")
    print("\nNothing has been approved. To decide:  POST /api/v1/opportunities/{id}/decision")
    return 0


if __name__ == "__main__":
    sys.exit(main())
