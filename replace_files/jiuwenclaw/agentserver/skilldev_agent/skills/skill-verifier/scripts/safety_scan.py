import json
import os
import random
import sys
import time
import warnings
from datetime import datetime
from jiuwenclaw.agentserver.open_ability_utils import get_oa_auth_headers

try:
    import requests
except ImportError:
    requests = None

try:
    from urllib3.exceptions import InsecureRequestWarning
except ImportError:
    InsecureRequestWarning = None

if InsecureRequestWarning is not None:
    warnings.filterwarnings("ignore", category=InsecureRequestWarning)

SKILL_SCAN_URL = os.getenv("SKILL_SCAN_URL")

COMMON_HEADERS = {
    "x-api-key": "apikey",
    "x-request-from": "jiuwenclaw",
    "Content-Type": "application/json",
}

_MAX_CONSECUTIVE_ERRORS = 3


def get_skill_scan_url():
    """Return the safety scan service URL, or fail if it is not configured."""
    if not SKILL_SCAN_URL:
        raise RuntimeError("SKILL_SCAN_URL environment variable is not set")
    return SKILL_SCAN_URL


def get_requests():
    """Return the requests module, delaying dependency errors until runtime."""
    if requests is None:
        raise RuntimeError("requests package is not installed")
    return requests


def _post_request(skill_id, payload):
    req = get_requests()
    auth_headers = get_oa_auth_headers()
    headers = {
        **COMMON_HEADERS,
        "x-skill-id": skill_id,
    }
    headers.update(auth_headers)

    try:
        resp = req.post(
            get_skill_scan_url(),
            headers=headers,
            json=payload,
            timeout=30,
            verify=False,
        )
        resp.raise_for_status()
        return resp.json()
    except req.exceptions.RequestException as e:
        if hasattr(e, "response") and e.response is not None:
            raise RuntimeError(
                f"{skill_id} request failed: "
                f"status={e.response.status_code}, body={e.response.text}"
            ) from e
        raise


def _get_conclusion(raw_result):
    if not isinstance(raw_result, dict):
        return None
    if "conclusion" in raw_result:
        return raw_result.get("conclusion")
    results = raw_result.get("data", {}).get("result", [])
    if results:
        return results[0].get("conclusion")
    return None


def _format_failure(raw_result):
    if not isinstance(raw_result, dict):
        return json.dumps(raw_result, indent=2, ensure_ascii=False)

    if "error" in raw_result and "last_response" in raw_result:
        last_resp = raw_result.get("last_response")
        last_resp_str = (
            last_resp
            if isinstance(last_resp, str)
            else json.dumps(last_resp, indent=2, ensure_ascii=False)
        )
        return "\n".join(
            [
                f"reason：{raw_result.get('error')}",
                f"maxAttempts：{raw_result.get('maxAttempts')}",
                f"intervalSeconds：{raw_result.get('intervalSeconds')}",
                f"lastResponse：{last_resp_str}",
            ]
        )

    scan = None
    if any(k in raw_result for k in ("conclusion", "modelResult", "items")):
        scan = raw_result
    else:
        inner = raw_result.get("data", {}).get("result", [])
        if inner:
            scan = inner[0]

    if scan:
        def _fmt(v):
            return v if isinstance(v, str) else json.dumps(v, indent=2, ensure_ascii=False)

        return "\n".join(
            [
                f"conclusion：{_fmt(scan.get('conclusion'))}",
                f"modelResult：{_fmt(scan.get('modelResult'))}",
                f"items：{_fmt(scan.get('items'))}",
            ]
        )

    return json.dumps(raw_result, indent=2, ensure_ascii=False)


def scan_url(skill_name, url, max_attempts=20, interval=3):
    """Submit scan task, poll for status, return result dict."""
    req_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    try:
        submit_data = _post_request(
            "skill_scan",
            {
                "skillName": skill_name,
                "skillID": "skill_scan",
                "action": "scan",
                "url": url,
                "reqTime": req_time,
            },
        )
    except Exception as e:
        return {"error": str(e)}

    if submit_data.get("code") != "0":
        return submit_data

    task_id = submit_data.get("data", {}).get("data", {}).get("taskID")
    if not task_id:
        return submit_data

    time.sleep(3)
    last_data = None
    consecutive_errors = 0
    for attempt in range(1, max_attempts + 1):
        try:
            last_data = _post_request("skill_scan_query_task", {"taskID": task_id})
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                return {
                    "error": (
                        f"Service unavailable: {consecutive_errors} consecutive "
                        f"poll failures, aborting."
                    ),
                    "maxAttempts": max_attempts,
                    "attemptReached": attempt,
                    "last_response": last_data,
                }
            if attempt >= max_attempts:
                raise
            time.sleep(interval + random.uniform(0, 1))
            continue

        results = last_data.get("data", {}).get("result", [])
        if results and results[0].get("reportStatus") == "COMPLETED":
            return last_data

        if attempt < max_attempts:
            time.sleep(interval + random.uniform(0, 1))

    return {
        "error": "Safety scan did not complete before polling timed out.",
        "maxAttempts": max_attempts,
        "intervalSeconds": interval,
        "last_response": last_data,
    }


def main(argv):
    if len(argv) != 3:
        print("Safety scan failed:")
        print("Usage: python3 -m scripts.safety_scan <skill-name> <url>")
        return 2

    raw_result = scan_url(skill_name=argv[1], url=argv[2])
    conclusion = _get_conclusion(raw_result)

    if str(conclusion).upper() == "BENIGN":
        print("Safety scan passed.")
        return 0

    print("Safety scan failed:")
    print(_format_failure(raw_result))
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
