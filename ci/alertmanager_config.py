"""Rendre les configurations de supervision du pilote avec les memes sources qu Ansible."""
import json
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'ansible/roles/monitoring'


def values(**overrides):
    result = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
    for name in ('application-monitoring', 'container-monitoring', 'alertmanager'):
        result.update(yaml.safe_load((ROOT / f'ansible/inventories/group_vars/moodle/{name}.yml').read_text()))
    result.update(monitoring_container_textfile_dir='/var/lib/moodle-container-monitoring/textfile',
                  monitoring_application_php_port=9253, monitoring_application_postgres_port=9187)
    result.update(overrides)
    return result


def render(name, parameters=None):
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    environment.filters.update(bool=bool, to_json=json.dumps)
    template = environment.from_string((ROLE / f'templates/{name}.yml.j2').read_text())
    return yaml.safe_load(template.render(parameters or values()))
