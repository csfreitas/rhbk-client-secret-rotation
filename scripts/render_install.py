#!/usr/bin/env python3
"""Render the same OpenShift installation templates for every environment.

This command is local-only: it does not connect to a cluster, create Secrets,
approve an InstallPlan or deploy workloads. Use the rendered files with oc.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / 'deploy/install'
COMPONENTS = ('operator', 'build', 'keycloak', 'postgres-demo')
CONNECTION_PARAMETERS = {'KUBECONFIG', 'CONTEXT', 'EXPECTED_SERVER', 'RHBK_CA_FILE'}
IMAGE_PARAMETERS = {'RHBK_BASE_IMAGE', 'RHBK_IMAGE', 'PG_IMAGE'}
NAME_PARAMETERS = {
    'NAMESPACE', 'RHBK_NAME', 'OPERATOR_GROUP', 'OPERATOR_CATALOG',
    'OPERATOR_CATALOG_NAMESPACE', 'BUILD_NAME', 'DB_USERNAME_SECRET',
    'DB_PASSWORD_SECRET', 'BOOTSTRAP_SECRET', 'PG_NAME',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_parameters(path):
    """Accept plain KEY=value, shared with oc --param-file and shell sourcing."""
    values = {}
    known = set(CONNECTION_PARAMETERS)
    for name in COMPONENTS:
        template = json.loads((TEMPLATES / (name + '.json')).read_text())
        known.update(p['name'] for p in template['parameters'])
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        require(separator and key in known, f'Unknown parameter at line {number}')
        require(key not in values, f'Duplicate parameter: {key}')
        # Do not accept executable shell expressions or ambiguous quoted values.
        require(not any(c.isspace() or c in '\\"\'`$;|&<>(){}' for c in value),
                f'{key} must be a plain value without spaces or shell expressions')
        values[key] = value
    return values


def validate(values):
    for key, value in values.items():
        require(value and 'REPLACE' not in value, f'Fill in {key}')
        if key in IMAGE_PARAMETERS:
            require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}', value),
                    f'{key} must be an image reference pinned by sha256 digest')
        if key in NAME_PARAMETERS:
            require(len(value) <= 63 and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', value),
                    f'{key} must be a DNS label of at most 63 characters')
        if key == 'ARCHITECTURE':
            require(value in {'amd64', 'arm64'}, 'ARCHITECTURE must be amd64 or arm64')
        if key == 'RHBK_INSTANCES':
            require(value.isdigit() and 1 <= int(value) <= 100, 'RHBK_INSTANCES must be between 1 and 100')
        if key == 'DB_PORT':
            require(value.isdigit() and 1 <= int(value) <= 65535, 'DB_PORT is invalid')
        if key == 'RHBK_HOSTNAME':
            require(len(value) <= 253 and '.' in value and all(
                len(part) <= 63 and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', part)
                for part in value.split('.')), 'RHBK_HOSTNAME must be a DNS hostname without scheme or path')


def render_component(name, values):
    path = TEMPLATES / (name + '.json')
    template = json.loads(path.read_text())
    selected = {}
    for parameter in template['parameters']:
        key = parameter['name']
        value = values.get(key, parameter.get('value', ''))
        if parameter.get('required'):
            require(value, f'Missing parameter: {key}')
        if value:
            selected[key] = value
    validate(selected)
    command = ['oc', '--kubeconfig=' + os.devnull, 'process', '--local=true',
               '-f', str(path), '-o', 'json']
    for key, value in selected.items():
        command.extend(['-p', key + '=' + value])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    require(result.returncode == 0, f'oc could not process the {name} template; check its parameters and oc version')
    resource = json.loads(result.stdout)
    require(resource.get('kind') == 'List' and resource.get('items'), 'Expected a nonempty resource List')
    require('${' not in json.dumps(resource), 'Unresolved template parameter')
    return resource


def render(params, components, output, image=None):
    values = read_parameters(params)
    if image is not None:
        values['RHBK_IMAGE'] = image
    rendered = {name: render_component(name, values) for name in components}
    output = Path(output)
    require(not output.is_symlink(), 'Output directory must not be a symlink')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, resource in rendered.items():
        destination = output / (name + '.json')
        require(not destination.is_symlink(), 'Output file must not be a symlink')
        descriptor, temporary = tempfile.mkstemp(dir=output, prefix='.' + name)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(resource, stream, indent=2)
                stream.write('\n')
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print(destination)
    print('Rendered locally; no cluster resources changed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--params', default=str(ROOT / '.private/rhbk-install.env'))
    parser.add_argument('--component', choices=COMPONENTS, action='append')
    parser.add_argument('--image', help='Completed Build image by digest, used for the Keycloak CR')
    parser.add_argument('--output', default=str(ROOT / '.private/rhbk-install'))
    args = parser.parse_args()
    try:
        render(args.params, args.component or ['operator', 'build'], args.output, args.image)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, 'BLOCKED: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
