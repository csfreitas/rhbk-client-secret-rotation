#!/usr/bin/env python3
"""Offline review of captured T05 events; raw evidence is never rewritten.

The initial harness accepted only invalid_client. RHBK 26.6.9 was observed to
return unauthorized_client for expired/invalidated secrets. Reclassify only
those credential-rejection expectations with the required temporal and positive
controls. Missing measurements remain BLOCKED; non-auth failures remain FAIL.
"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

SCENARIOS = ("early", "near", "expired", "control")
NEGATIVE_EVENTS = {"S1_after_rotated_expiration", "S2_after_own_expiration",
                   "S1_expired_before_POST", "S1_after_POST_without_policy"}


def good(event):
    return bool(event and event.get("http") == 200 and event.get("token_received") is True)


def rejected(event):
    return bool(event and event.get("http") in (400, 401) and
                event.get("oauth_error") in ("invalid_client", "unauthorized_client") and
                event.get("token_received") is False and event.get("category") == "http")


def review_scenario(rows, scenario):
    records = [row for row in rows if row.get("scenario") == scenario]
    indexed = {row.get("event"): row for row in records}
    base = {"scenario": scenario, "original_result": indexed.get("conclusion", {}).get("result", "NOT_RUN"),
            "corrections": [], "failed_checks": [], "missing_checks": []}
    if not indexed.get("conclusion"):
        return {**base, "result": "BLOCKED", "reason": "Scenario has no captured final conclusion; do not infer missing measurements"}
    if len([row for row in records if row.get("event") == "S1_initialized"]) != 1:
        return {**base, "result": "BLOCKED", "reason": "Ambiguous or missing client initialization"}
    initialization, post = indexed.get("S1_initialized", {}), indexed.get("POST_metadata", {})
    epsilon = initialization.get("epsilon")
    if not isinstance(epsilon, (int, float)) or not post:
        return {**base, "result": "BLOCKED", "reason": "Required timing metadata missing"}
    e1 = initialization.get("original_expiration")
    e2, old_exp = post.get("expiration_after_post"), post.get("rotated_expiration")

    def has(event_name):
        if event_name not in indexed:
            base["missing_checks"].append(event_name)
            return False
        return True

    def positive(event_name):
        return has(event_name) and good(indexed[event_name])

    def after(event_name, deadline):
        if not has(event_name) or not isinstance(deadline, (int, float)):
            return False
        value = indexed[event_name].get("epoch")
        return isinstance(value, (int, float)) and value > deadline+epsilon

    def before(event_name, deadline):
        if not has(event_name) or not isinstance(deadline, (int, float)):
            return False
        value = indexed[event_name].get("epoch")
        return isinstance(value, (int, float)) and value < deadline-epsilon

    def recorded_pass(event_name):
        return has(event_name) and indexed[event_name].get("result") == "PASS"

    controls = {
        "S1_after_rotated_expiration": lambda: positive("baseline") and
            (scenario == "expired" or positive("S1_before_rotated_expiration")) and
            after("S1_after_rotated_expiration", old_exp) and
            positive("S2_after_old_expiration") and after("S2_after_old_expiration", old_exp),
        "S2_after_own_expiration": lambda: positive("S2_before_own_expiration") and
            before("S2_before_own_expiration", e2) and after("S2_after_own_expiration", e2) and
            recorded_pass("no_additional_secret_mutations"),
        "S1_expired_before_POST": lambda: positive("baseline") and
            after("S1_expired_before_POST", e1) and positive("S2_immediately_after_POST") and
            recorded_pass("no_rotation_due_to_time_alone"),
        "S1_after_POST_without_policy": lambda: positive("baseline") and positive("S2_immediately_after_POST") and
            e1 is None and e2 is None,
    }
    required_negative = ["S1_after_POST_without_policy"] if scenario == "control" else [
        "S1_after_rotated_expiration", "S2_after_own_expiration"]
    if scenario == "expired":
        required_negative.append("S1_expired_before_POST")
    for event_name in required_negative:
        if not has(event_name):
            continue
        record = indexed[event_name]
        accepted = rejected(record) and controls[event_name]()
        if not accepted:
            base["failed_checks"].append(event_name+": rejection or required controls not proven")
        elif record.get("result") == "FAIL":
            base["corrections"].append({"event": event_name, "original_result": "FAIL", "review_result": "PASS",
                "http": record.get("http"), "oauth_error": record.get("oauth_error"), "epoch": record.get("epoch"),
                "reason": "Observed credential rejection accepted only with preserved timed and positive controls"})
    # Preserve all unrelated failures: only the documented predicate is corrected.
    for event_name, record in indexed.items():
        if event_name != "conclusion" and event_name not in NEGATIVE_EVENTS and record.get("result") in ("FAIL", "BLOCKED"):
            base["failed_checks"].append(event_name+": original non-reclassified failure")
    if not positive("baseline") or not positive("S2_immediately_after_POST"):
        base["failed_checks"].append("Fresh authentication baseline missing or failed")
    if scenario == "control":
        if not recorded_pass("control_no_expiration_assigned") or not positive("S2_beyond_policy_TTL_without_policy"):
            base["failed_checks"].append("No-policy comparison not proven")
        renewal = "NOT_APPLICABLE: no server-assigned principal expiration"
    else:
        for name in ("initial_TTL", "E2_matches_POST_plus_TTL", "E2_advanced_over_E1_original", "no_additional_secret_mutations"):
            if not recorded_pass(name):
                base["failed_checks"].append(name+": original metadata/control check not PASS")
        proof_event = "S2_immediately_after_POST" if scenario == "expired" else "S2_beyond_original_expiration"
        if not (positive(proof_event) and after(proof_event, e1) and before(proof_event, e2)):
            base["failed_checks"].append("S2 validity after E1_original and before E2 not proven")
        if scenario == "early":
            if not recorded_pass("old_expiration_matches_rotation_window"):
                base["failed_checks"].append("Original old-window metadata check not PASS")
            # The original run's configured update window is 60 seconds.
            if not isinstance(e1, (int, float)) or e1-post.get("post_start", e1) <= 60:
                base["failed_checks"].append("Early POST outside update window not proven")
        renewal = "Confirmed by preserved metadata and new authentications" if not base["failed_checks"] and not base["missing_checks"] else "Not confirmed"
    status = "BLOCKED" if base["missing_checks"] else "FAIL" if base["failed_checks"] else "PASS"
    return {**base, "result": status, "post_renewal": renewal,
            "E1_original": e1, "E2": e2, "E1_rotated": old_exp,
            "post_start": post.get("post_start"), "post_end": post.get("post_end"),
            "old_secret_rule": indexed.get("old_secret_expiration_rule", {}).get("result"),
            "client_id": initialization.get("client_id")}


def review_file(source, output):
    source, output = Path(source), Path(output)
    raw = source.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    results = [review_scenario(rows, scenario) for scenario in SCENARIOS]
    document = {"reviewed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": str(source.resolve()), "source_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_modified": False, "rule_change": "Credential rejection includes unauthorized_client with HTTP400/401 and required controls",
        "builds": sorted({row.get("build", "NOT_MEASURED") for row in rows}),
        "review_expected_remaining_window_seconds": 60,
        "results": results, "result": "FAIL" if any(item["result"] == "FAIL" for item in results) else "BLOCKED" if any(item["result"] == "BLOCKED" for item in results) else "PASS"}
    output.mkdir(parents=True, exist_ok=True)
    (output/"review.json").write_text(json.dumps(document, indent=2)+"\n")
    lines = ["# Revisão das evidências do POST", "", "Os eventos originais não foram alterados.", "",
             "O predicado original aceitava apenas `invalid_client`. A revisão aceita também `unauthorized_client` em HTTP 400/401 somente com os controles temporais e positivos registrados. Falhas de rede e demais falhas não são reclassificadas.", "",
             "SHA-256 do arquivo original: `"+document["source_sha256"]+"`.", "",
             "| Cenário | Resultado original | Revisão | Conclusão |", "|---|---|---|---|"]
    for result in results:
        lines.append("| "+result["scenario"]+" | "+result["original_result"]+" | "+result["result"]+" | "+result.get("post_renewal", result.get("reason", ""))+" |")
        for item in result["corrections"]:
            lines.append("\n- "+result["scenario"]+" / "+item["event"]+": FAIL original → PASS na revisão; HTTP "+str(item["http"])+", OAuth `"+item["oauth_error"]+"`; controles preservados.")
        for item in result["failed_checks"]+result["missing_checks"]:
            lines.append("\n- "+result["scenario"]+": "+item)
    (output/"review.md").write_text("\n".join(lines)+"\n")
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Original sanitized JSONL file, read-only")
    parser.add_argument("--output", required=True, help="Separate review directory")
    args = parser.parse_args()
    document = review_file(args.source, args.output)
    print(json.dumps({"result": document["result"], "source_sha256": document["source_sha256"],
                      "scenarios": [{"scenario": item["scenario"], "original": item["original_result"], "review": item["result"]} for item in document["results"]]}))
    return 0 if document["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
