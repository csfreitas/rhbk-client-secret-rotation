"""Configured OpenShift namespace/client boundaries; no network or cluster calls."""
import base64
import contextlib
import copy
import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from test_controller import c, Clock, Controller, FakeKube, HeldLease, KC, State


class ControllerPortability(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.environment.stop()

    def secret(self, ctl, *, client=None, generation=1):
        name = c.versioned_secret(ctl.secret_prefix, generation)
        return {"metadata": {"name": name, "labels": copy.deepcopy(c.LABELS)}, "immutable": True,
                "data": {key: base64.b64encode(value.encode()).decode() for key, value in {
                    "client-secret": ctl.fakekc.current, "client-id": client or ctl.client,
                    "generation": c.generation_label(generation)}.items()}}

    def test_namespace_is_configurable_without_changing_tls_or_ownership(self):
        with patch.dict(os.environ, {"NAMESPACE": "team-payments", "KUBERNETES_SERVICE_HOST": "api.internal"}), patch.object(c, "HTTP") as http:
            kube = c.Kube()
            self.assertEqual(kube.core, "/api/v1/namespaces/team-payments")
            self.assertEqual(http.call_args.args[0], "https://api.internal:443")
            self.assertTrue(http.call_args.args[1].endswith("/ca.crt"))
            with self.assertRaises(c.SafeError):kube.owned({"metadata": {"labels": {}}})

    def test_invalid_namespace_never_constructs_http_client(self):
        for namespace in ("../other", "UpperCase", "two.names", "x"*64, "", "other?query=1"):
            with self.subTest(namespace=namespace), patch.dict(os.environ, {"NAMESPACE": namespace}), patch.object(c, "HTTP") as http:
                with self.assertRaises(c.SafeError):c.Kube()
                http.assert_not_called()

    def test_kubernetes_service_url_handles_ipv6_ipv4_and_dns_with_tls(self):
        for host, expected in (("fd00:10:96::1", "https://[fd00:10:96::1]:6443"),
                               ("[fd00:10:96::1]", "https://[fd00:10:96::1]:6443"),
                               ("10.96.0.1", "https://10.96.0.1:6443"),
                               ("kubernetes.default.svc", "https://kubernetes.default.svc:6443")):
            with self.subTest(host=host), patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": host, "KUBERNETES_SERVICE_PORT": "6443"}), patch.object(c, "HTTP") as http:
                c.Kube()
                self.assertEqual(http.call_args.args[0], expected)
                self.assertTrue(http.call_args.args[1].endswith("/ca.crt"))

    def test_invalid_service_address_is_rejected_before_http_setup(self):
        for host, port in (("api.example:443", "443"), ("api.example/path", "443"),
                           ("user@api.example", "443"), ("fd00::1", "0"), ("fd00::1", "65536")):
            with self.subTest(host=host, port=port), patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": host, "KUBERNETES_SERVICE_PORT": port}), patch.object(c, "HTTP") as http:
                with self.assertRaises(c.SafeError):c.Kube()
                http.assert_not_called()

    def test_custom_client_and_prefix_complete_cycle_and_bind_persisted_state(self):
        with patch.dict(os.environ, {"TARGET_CLIENT_ID": "billing-service", "SECRET_PREFIX": "billing-credentials", "NAMESPACE": "team-billing", "REALM": "services", "CRONJOB_NAME": "rotate-billing"}):
            ctl = Controller(); ctl.run()
            self.assertEqual(ctl.fakekc.posts, 1)
            self.assertEqual(ctl.state.value["current_secret"], "billing-credentials-v0002")
            self.assertEqual(ctl.state.value["target_client_id"], "billing-service")
            self.assertEqual(ctl.state.value["secret_prefix"], "billing-credentials")
            self.assertEqual(ctl.state.value["realm"], "services")
            self.assertEqual(ctl.state.value["namespace"], "team-billing")
            self.assertEqual(ctl.state.value["phase"], "COMPLETE")

    def test_custom_prefix_crash_reconciles_same_generation_without_second_post(self):
        with patch.dict(os.environ, {"TARGET_CLIENT_ID": "billing-service", "SECRET_PREFIX": "billing"}):
            ctl = Controller()
            with patch.dict(os.environ, {"INJECT_FAILURE": "after-post"}), self.assertRaises(c.SafeError):ctl.run()
            ctl.run(reconcile_only=True)
            self.assertEqual(ctl.fakekc.posts, 1)
            self.assertEqual(ctl.state.value["current_secret"], "billing-v0002")

    def test_new_default_interval_is_300_and_remains_configurable(self):
        for explicit, expected in ((None, 300), ("180", 180), ("600", 600)):
            with self.subTest(explicit=explicit), patch.dict(os.environ, {} if explicit is None else {"ROTATE_INTERVAL": explicit}):
                clock = Clock()
                ctl = c.Controller(None, KC(clock), State(), HeldLease(), clock.time, clock.sleep)
                self.assertEqual(ctl.interval, expected)

    def test_nonpositive_interval_and_unsafe_configured_names_are_rejected(self):
        for key, value in (("ROTATE_INTERVAL", "0"), ("ROTATE_INTERVAL", "-1"), ("TARGET_CLIENT_ID", ""),
                           ("TARGET_CLIENT_ID", "client\nother"), ("SECRET_PREFIX", "../secret"),
                           ("SECRET_PREFIX", "p"*64), ("CRONJOB_NAME", "c"*53),
                           ("CRONJOB_NAME", "cron/other"), ("DEPLOYMENT_NAME", "../deployment")):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}), self.assertRaises(c.SafeError):Controller()

    def test_state_target_namespace_realm_or_prefix_change_is_rejected(self):
        for key, value in (("target_client_id", "other-client"), ("namespace", "other-namespace"),
                           ("realm", "other-realm"), ("secret_prefix", "other-prefix")):
            with self.subTest(key=key):
                state = State(); state.value[key] = value; clock = Clock(); kc = KC(clock)
                with self.assertRaises(c.SafeError):c.Controller(None, kc, state, HeldLease(), clock.time, clock.sleep)
                self.assertEqual(kc.posts, 0)

    def test_malformed_or_cross_prefix_state_reference_blocks_before_rotation(self):
        for changed in ({"current_secret": "foreign-v0001"}, {"generation": True},
                        {"next_generation": 2, "next_secret": "catalogo-api-cred-v0003"},
                        {"old_generation": 1, "old_secret": "foreign-v0001"}):
            with self.subTest(changed=changed):
                ctl = Controller(); ctl.state.value.update(changed)
                with self.assertRaises(c.SafeError):ctl.run()
                self.assertEqual(ctl.fakekc.posts, 0)

    def test_legacy_state_without_binding_accepts_only_matching_immutable_secret(self):
        ctl = Controller(); ctl.kube = FakeKube()
        obj = self.secret(ctl); ctl.kube.create(ctl.kube.core+"/secrets", obj)
        self.assertEqual(c.Controller.credential(ctl, obj["metadata"]["name"]), ctl.fakekc.current)

    def test_credential_read_rejects_wrong_client_generation_and_mutable_object(self):
        cases = [lambda obj: obj["data"].update({"client-id": base64.b64encode(b"other-client").decode()}),
                 lambda obj: obj["data"].update({"generation": base64.b64encode(b"v0002").decode()}),
                 lambda obj: obj.update({"immutable": False}),
                 lambda obj: obj["data"].pop("client-id"),
                 lambda obj: obj["metadata"]["labels"].update({"lab.rhbk/generation": "v0002"}),
                 lambda obj: obj["metadata"].update({"labels": {}})]
        for change in cases:
            ctl = Controller(); ctl.kube = FakeKube(); obj = self.secret(ctl); change(obj)
            ctl.kube.create(ctl.kube.core+"/secrets", obj)
            with self.assertRaises(c.SafeError):c.Controller.credential(ctl, obj["metadata"]["name"])
            self.assertEqual(ctl.fakekc.posts, 0)

    def test_credential_name_is_canonical_not_just_a_prefix_match(self):
        ctl = Controller(); ctl.kube = Mock()
        for name in ("catalogo-api-cred-v0001-other", "catalogo-api-cred-v00001", "other-v0001", "catalogo-api-cred-v0000"):
            with self.subTest(name=name), self.assertRaises(c.SafeError):c.Controller.credential(ctl, name)
        ctl.kube.get.assert_not_called()

    def test_staged_secret_with_same_value_and_wrong_client_blocks(self):
        ctl = Controller(); ctl.kube = FakeKube()
        ctl.state.save(next_generation=2, next_secret="catalogo-api-cred-v0002")
        obj = self.secret(ctl, client="other-client", generation=2)
        ctl.kube.create(ctl.kube.core+"/secrets", obj)
        with self.assertRaises(c.SafeError):c.Controller.stage(ctl, ctl.fakekc.current)
        self.assertEqual(ctl.state.value["phase"], "BLOCKED")

    def test_custom_cronjob_is_used_for_resume(self):
        kube = FakeKube(); kube.batch = "/batch"
        kube.create(kube.core+"/configmaps", {"metadata": {"name": "rotation-state", "labels": c.LABELS}, "data": {"state.json": json.dumps(State().value)}})
        kube.create(kube.batch+"/cronjobs", {"metadata": {"name": "rotate-orders", "labels": c.LABELS}, "spec": {"suspend": True}})
        lease = Mock(); factory = Mock(); factory.acquire.return_value = lease
        with patch.dict(os.environ, {"CRONJOB_NAME": "rotate-orders"}), patch.object(c, "Kube", return_value=kube), patch.object(c, "Lease", return_value=factory), patch.object(c.sys, "argv", ["controller.py", "resume"]):
            self.assertEqual(c.main(), 0)
        self.assertFalse(kube.get(kube.batch+"/cronjobs/rotate-orders")["spec"]["suspend"])
        lease.close.assert_called_once()

    def test_custom_target_prefix_and_cronjob_are_used_for_expired_recovery(self):
        with patch.dict(os.environ, {"TARGET_CLIENT_ID": "billing", "SECRET_PREFIX": "billing-cred", "CRONJOB_NAME": "rotate-billing"}):
            ctl = Controller(); ctl.fakeclock.now = 310; ctl.state.value["suspended"] = True
            kube = FakeKube(); kube.batch = "/batch"; kube.apps = "/apps"; ctl.kube = kube
            kube.create(kube.batch+"/cronjobs", {"metadata": {"name": "rotate-billing", "labels": c.LABELS}, "spec": {"suspend": True}})
            deployment = {"metadata": {"name": ctl.deployment, "labels": c.LABELS}, "spec": {"template": {
                "metadata": {"labels": {"app.kubernetes.io/name": "m2m-probe", "lab.rhbk/generation": "v0001"}},
                "spec": {"containers": [{"name": "probe", "env": [{"name": key, "valueFrom": {"secretKeyRef": {"name": "billing-cred-v0001", "key": value}}} for key, value in (("CLIENT_SECRET", "client-secret"), ("CLIENT_ID", "client-id"), ("GENERATION", "generation"))]}]}}}}
            kube.create(kube.apps+"/deployments", deployment)
            ctl.recover_expired()
            self.assertEqual(ctl.fakekc.posts, 1)
            self.assertEqual(ctl.fakekc.deletes, 1)
            self.assertEqual(ctl.state.value["current_secret"], "billing-cred-v0002")
            self.assertEqual(ctl.state.value["target_client_id"], "billing")
            self.assertEqual(ctl.state.value["phase"], "COMPLETE")

    def test_non_dns_client_identifier_is_encoded_and_matched_exactly(self):
        client = "billing/api v2"
        self.assertEqual(c.client_identifier(client), client)
        kc = object.__new__(c.Keycloak)
        kc.admin = Mock(return_value=(200, [{"id": "client-uuid", "clientId": client}], {}))
        self.assertEqual(kc.resolve(client), "client-uuid")
        kc.admin.assert_called_once_with("GET", "/clients?clientId=billing%2Fapi%20v2&exact=true")

    def test_state_and_lease_names_cannot_escape_rest_path(self):
        kube = Mock(); kube.coordination = "/coordination"; kube.core = "/core"
        with self.assertRaises(c.SafeError):c.Lease(kube, "name/other")
        with self.assertRaises(c.SafeError):c.State(kube, "../other")
        kube.get.assert_not_called()

    def test_foreign_deployment_credential_reference_is_rejected(self):
        ctl = Controller(); kube = FakeKube(); kube.apps = "/apps"; ctl.kube = kube
        obj = {"metadata": {"name": ctl.deployment, "labels": c.LABELS}, "spec": {"template": {
            "metadata": {"labels": {"app.kubernetes.io/name": "m2m-probe", "lab.rhbk/generation": "v0001"}},
            "spec": {"containers": [{"name": "probe", "env": [{"name": key, "valueFrom": {"secretKeyRef": {"name": "another-client-v0001", "key": value}}} for key, value in (("CLIENT_SECRET", "client-secret"), ("CLIENT_ID", "client-id"), ("GENERATION", "generation"))]}]}}}}
        kube.create(kube.apps+"/deployments", obj)
        with self.assertRaises(c.SafeError):c.Controller.deployment_object(ctl)
        self.assertEqual(ctl.fakekc.posts, 0)

    def test_old_probe_deadline_covers_configured_grace_and_keeps_default(self):
        for grace, margin, expected in ((120, 15, 360), (600, 15, 720), (2000, 300, 2315)):
            with self.subTest(grace=grace, margin=margin), patch.dict(os.environ, {"ROTATED_TTL": str(grace), "MARGIN_SECONDS": str(margin)}):
                ctl = Controller(); ctl.kube = FakeKube()
                ctl.deployment_object = lambda: {"spec": {"template": {"metadata": {"labels": {}}, "spec": {"containers": [{"name": "probe", "env": []}]}}}}
                c.Controller.old_probe(ctl, 1)
                pod = ctl.kube.get(ctl.kube.core+"/pods/old-probe-v0001")
                self.assertEqual(pod["spec"]["activeDeadlineSeconds"], expected)
                self.assertEqual(pod["spec"]["restartPolicy"], "Never")


if __name__ == '__main__':unittest.main()
