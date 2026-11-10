#!/usr/bin/env python3
"""Separate external-value PUT experiment. Never used by the rotation controller.

PASS means the comparison was observed with its preconditions satisfied. It does
not assert that PUT renews expiration or behaves like POST. See each observation.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

spec = importlib.util.spec_from_file_location("post_expiration", Path(__file__).with_name("post_expiration.py"))
exp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exp)

SCENARIOS = ("policy-before", "policy-after", "control-before", "control-after")


class Evidence(exp.Evidence):
    FIELDS = [field.replace("expiration_after_post", "expiration_after_put") for field in exp.Evidence.FIELDS]

    def finish(self):
        summary = super().finish()
        summary["mutation"] = "PUT supplied externally-generated secret"
        summary["comparison_to_POST"] = "No equivalence assumed"
        (self.directory/"summary.json").write_text(json.dumps(summary, indent=2)+"\n")
        lines = ["# Comparação separada: geração externa e PUT", "",
                 "Build efetiva: `"+self.build+"`.", "",
                 "PASS indica que os controles e observações foram executados. Não significa renovação de prazo nem equivalência com POST.", "",
                 "O valor externo e os tokens foram mantidos apenas em memória. Nenhum timestamp interno foi enviado ao servidor.", "",
                 "| Cenário | Estado | Observação / bloqueio |", "|---|---|---|"]
        for item in self.results:
            note = item.get("reason", item.get("note", "Ver eventos abaixo"))
            lines.append("| "+item["scenario"]+" | "+item["result"]+" | "+str(note)+" |")
        lines += ["", "| Cenário | Evento | UTC | Geração | Expiração anterior | Expiração após PUT | Expiração rotacionada | HTTP | OAuth | Estado |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for item in self.events:
            keys = ("scenario", "event", "timestamp", "generation", "original_expiration",
                    "expiration_after_put", "rotated_expiration", "http", "oauth_error", "result")
            lines.append("| "+" | ".join(str(item.get(key, "")) for key in keys)+" |")
        (self.directory/"report.md").write_text("\n".join(lines)+"\n")
        return summary


class Comparison(exp.Experiment):
    def run(self, scenario, before_delay=5):
        policy = scenario.startswith("policy-")
        after = scenario.endswith("-after")
        self.setup("put-"+scenario, policy=policy)
        if not policy:
            self.check("control_has_no_server_deadline", self.e1 is None)
            if self.e1 is not None:
                return self.end(note="Control unexpectedly has a server-assigned expiration; comparison not applicable")
        if after:
            # For controls this is ONLY a matched elapsed-time reference, not an expiry.
            deadline = self.original_deadline if policy else self.init["end"]+self.ttl
            self.ev.add(self.scenario, "wait_for_precondition", result="OBSERVED",
                        server_deadline=self.e1 if policy else None,
                        elapsed_reference_deadline=None if policy else deadline,
                        note="No expiration is attributed to the no-policy control" if not policy else "Wait for measured principal expiry")
            self.wait(deadline+self.eps+1)
            result = self.auth("S1_before_PUT_after_wait", "S1", self.s1, "invalid" if policy else "valid")
            if policy and not exp.invalid_auth(result):
                return self.end(note="S1 rejection after deadline not observed; no PUT performed")
        else:
            if before_delay < 0 or before_delay+self.eps >= self.ttl:
                raise exp.Blocked("before_PUT_delay_must_precede_expiration")
            self.wait(self.init["end"]+before_delay)
            self.auth("S1_immediately_before_PUT", "S1", self.s1, "valid")
            if policy and time.time() >= self.original_deadline-self.eps:
                raise exp.Blocked("PUT_before_expiration_window_missed")
        before = self.api.get_meta(self.cid)
        externally_generated = secrets.token_urlsafe(32)
        # This partial representation intentionally contains NO attributes/timestamps.
        _, interval = self.api.admin("PUT", "/clients/"+self.cid, {"secret": externally_generated})
        current = self.api.secret(self.cid)
        observed = self.api.get_meta(self.cid)
        self.ev.add(self.scenario, "PUT_metadata", generation="supplied-external",
                    method="PUT", generation_source="external_CSPRNG",
                    put_start=interval["start"], put_end=interval["end"],
                    created_before=before["created"], created=observed["created"],
                    original_expiration=before["expires"], expiration_after_put=observed["expires"],
                    rotated_expiration=observed["rotated_expires"],
                    observed_delta=(observed["expires"]-before["expires"]) if observed["expires"] is not None and before["expires"] is not None else None,
                    supplied_value_is_current=current == externally_generated,
                    current_differs_from_S1=current != self.s1,
                    result="OBSERVED", no_internal_timestamps_sent=True)
        old_result = self.auth("S1_after_PUT", "S1", self.s1)
        supplied_result = self.auth("external_value_after_PUT", "external", externally_generated)
        current_result = self.auth("server_current_after_PUT", "server-current", current)
        self.check("no_followup_rotation_sent", self.api.secret(self.cid) == current)
        return self.end(note="PUT behavior observed; no equivalence to POST inferred",
                        supplied_value_is_current=current == externally_generated,
                        S1_auth_http=old_result["http"], supplied_auth_http=supplied_result["http"],
                        current_auth_http=current_result["http"], expiration_after_put=observed["expires"],
                        original_expiration=before["expires"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument("--output", default="evidence/put-comparison")
    parser.add_argument("--before-delay", type=float, default=5)
    args = parser.parse_args()
    build = os.environ.get("RHBK_SERVER_VERSION", "NOT_MEASURED")
    evidence = Evidence(args.output, build)
    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    try:
        required = ("RHBK_URL", "RHBK_CA_FILE", "ADMIN_CLIENT_SECRET_FILE")
        if any(not os.environ.get(key) for key in required):
            raise exp.Blocked("missing_runtime_configuration")
        realm = os.environ.get("REALM", "lab-secret-rotation")
        evidence.add("preflight", "CRC_guard", result="PASS", **exp.guard(os.environ["RHBK_URL"], realm))
        if not re.fullmatch(r"26\.6\.\d+(?:[.-].*)?", build):
            raise exp.Blocked("effective_RHBK_26_6_build_not_recorded")
        api = exp.API(os.environ["RHBK_URL"], os.environ["RHBK_CA_FILE"], realm,
                      os.environ.get("ADMIN_CLIENT_ID", "rotation-admin"), os.environ["ADMIN_CLIENT_SECRET_FILE"])
        api.access()
        comparison = Comparison(api, evidence, int(os.environ.get("TTL_PRINCIPAL", "300")),
                                int(os.environ.get("TTL_ROTATED", "120")),
                                int(os.environ.get("REMAINING_ROTATION", "60")),
                                int(os.environ.get("EPSILON", "5")))
        comparison.verify_profile()
        if api.http.clock_samples:
            offset = api.http.clock_samples[-1]["server_offset_seconds"]
            evidence.add("clock", "HTTP_Date_comparison", offset_seconds=offset)
            if abs(offset) > comparison.eps:
                raise exp.Blocked("host_RHBK_clock_difference_exceeds_epsilon")
        else:
            evidence.add("clock", "pod_clock_comparison", result="PASS", **exp.pod_clock_sample(build, comparison.eps))
            comparison.clock_checker = lambda: exp.pod_clock_sample(build, comparison.eps)
        for scenario in scenarios:
            try:
                comparison.run(scenario, args.before_delay)
            except exp.Blocked as exc:
                evidence.results.append({"scenario": "put-"+scenario, "result": "BLOCKED", "reason": str(exc)})
                evidence.add("put-"+scenario, "blocked", result="BLOCKED", reason=str(exc))
    except (exp.Blocked, OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, exp.Blocked) else type(exc).__name__
        for scenario in scenarios:
            evidence.results.append({"scenario": "put-"+scenario, "result": "BLOCKED", "reason": reason})
        evidence.add("preflight", "blocked", result="BLOCKED", reason=reason)
    summary = evidence.finish()
    return 0 if summary["results"] and all(item["result"] == "PASS" for item in summary["results"]) else 2


if __name__ == "__main__":
    sys.exit(main())
