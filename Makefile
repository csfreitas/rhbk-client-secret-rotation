PYTHON ?= python3
.PHONY: preflight setup mirror deploy build bootstrap baseline watch collect status rotate-once reconcile automation-start automation-stop recover-expired test test-periodic test-post-expiration test-put-comparison test-delayed-rollout test-recovery scan cleanup
.PHONY: watch-json history openshift-watch openshift-collect openshift-baseline openshift-status openshift-history openshift-start openshift-stop openshift-rotate-once openshift-reconcile
preflight:
	$(PYTHON) scripts/lab.py preflight
setup:
	$(PYTHON) scripts/setup.py install
mirror:
	$(PYTHON) scripts/mirror.py
deploy:
	$(PYTHON) scripts/lab.py deploy
build:
	$(PYTHON) scripts/lab.py build
bootstrap:
	$(PYTHON) scripts/bootstrap.py
baseline:
	$(PYTHON) scripts/observe.py baseline
watch:
	$(PYTHON) scripts/observe.py watch --output auto
watch-json:
	$(PYTHON) scripts/observe.py watch --format json
collect:
	$(PYTHON) scripts/observe.py collect
status:
	$(PYTHON) scripts/observe.py status
history:
	$(PYTHON) scripts/observe.py history
rotate-once:
	$(PYTHON) scripts/control.py rotate-once
reconcile:
	$(PYTHON) scripts/control.py reconcile
automation-start:
	$(PYTHON) scripts/control.py resume
automation-stop:
	$(PYTHON) scripts/control.py suspend
recover-expired:
	$(PYTHON) scripts/recover_expired.py
test:
	$(PYTHON) -m unittest discover -s tests/unit -v
test-periodic:
	$(PYTHON) tests/integration/periodic.py
test-post-expiration:
	$(PYTHON) scripts/runtime.py tests/integration/post_expiration.py --scenario all
test-put-comparison:
	$(PYTHON) scripts/runtime.py tests/integration/put_comparison.py
test-delayed-rollout:
	$(PYTHON) tests/integration/operations.py T07
test-recovery:
	$(PYTHON) tests/integration/operations.py T11
scan:
	$(PYTHON) scripts/secret_scan.py
cleanup:
	$(PYTHON) scripts/cleanup.py

# Portable operations use only the explicit configuration prepared by openshift.py.
openshift-watch:
	$(PYTHON) scripts/observe.py watch --environment openshift --output auto
openshift-collect:
	$(PYTHON) scripts/observe.py collect --environment openshift
openshift-baseline:
	$(PYTHON) scripts/observe.py baseline --environment openshift
openshift-status:
	$(PYTHON) scripts/observe.py status --environment openshift
openshift-history:
	$(PYTHON) scripts/observe.py history --environment openshift
openshift-start:
	$(PYTHON) scripts/control.py resume --environment openshift
openshift-stop:
	$(PYTHON) scripts/control.py suspend --environment openshift
openshift-rotate-once:
	$(PYTHON) scripts/control.py rotate-once --environment openshift
openshift-reconcile:
	$(PYTHON) scripts/control.py reconcile --environment openshift

# Maintenance of the original CRC installation; new installs use rhbk-render.
.PHONY: build-rhbk operator-prepare operator-approve operator-status
build-rhbk:
	$(PYTHON) scripts/rhbk_image.py build
operator-prepare:
	$(PYTHON) scripts/operator_olm.py prepare
operator-approve:
	$(PYTHON) scripts/operator_olm.py approve --plan "$(PLAN)"
operator-status:
	$(PYTHON) scripts/operator_olm.py check

# Common installation: the values file is the only environment selection.
PARAMS ?= .private/rhbk-install.env
.PHONY: rhbk-render rhbk-render-server rhbk-render-postgres
rhbk-render:
	$(PYTHON) scripts/render_install.py --params "$(PARAMS)"
rhbk-render-server:
	$(PYTHON) scripts/render_install.py --params "$(PARAMS)" --component keycloak --image "$(RHBK_IMAGE)"
rhbk-render-postgres:
	$(PYTHON) scripts/render_install.py --params "$(PARAMS)" --component postgres-demo

# Observe existing rotations with fresh token requests; never generate a secret.
WATCH_CONFIG ?= .private/watch-secrets.env
.PHONY: watch-secrets
watch-secrets:
	bash scripts/watch-secrets.sh --config "$(WATCH_CONFIG)"
