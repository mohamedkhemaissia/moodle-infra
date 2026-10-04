#!/usr/bin/python3
"""Map the saved Compose configuration to distinct recovery resources."""
import json
import re
import sys
from pathlib import Path

import yaml


def require(condition, message):
    if not condition:
        raise ValueError(message)


def generate(saved, destination, old_base, new_base, old_project, project, version, wwwroot):
    old = Path(old_base)
    new = Path(new_base)
    require(old.is_absolute() and new.is_absolute(), 'Chemins absolus obligatoires.')
    require(old != new and old not in new.parents and new not in old.parents,
            'Les repertoires du pilote et de la copie doivent etre distincts.')
    require(project != old_project and re.fullmatch(r'[a-z0-9][a-z0-9_-]+', project),
            'Nom Compose de restauration invalide.')
    config = yaml.safe_load(Path(saved).read_text())
    require(isinstance(config, dict), 'Configuration Compose absente.')
    services = config.get('services', {})
    require(set(services) == {'apache', 'php', 'postgres'}, 'Trois services attendus.')
    require(set(config.get('volumes', {})) == {'postgres_data', 'moodledata'},
            'Volumes de sauvegarde inattendus.')
    require(set(config.get('networks', {})) == {'public', 'private'},
            'Reseaux de sauvegarde inattendus.')
    require(set(config.get('secrets', {})) == {'postgres_admin_password', 'moodle_db_password'},
            'Secrets de sauvegarde inattendus.')

    def mapped(value):
        path = Path(value)
        require(path.is_absolute() and '..' not in path.parts,
                'Chemin de montage invalide.')
        relative = path.relative_to(old)
        target = new / relative
        require(target.exists(), 'Fichier restaure absent : ' + str(target))
        return str(target)

    code = new / 'releases' / version / 'moodle'
    mounts = []
    images = []
    for name, service in services.items():
        require('build' not in service and 'container_name' not in service,
                'La copie utilise des images locales et des noms Compose distincts.')
        require(isinstance(service.get('image'), str), 'Image absente : ' + name)
        images.append(service['image'])
        for mount in service.get('volumes', []):
            # The archived template uses short bind and volume syntax.
            require(isinstance(mount, str), 'Format de montage inattendu.')
            parts = mount.split(':')
            require(len(parts) in (2, 3), 'Montage invalide.')
            if parts[0].startswith('/'):
                parts[0] = mapped(parts[0])
            else:
                require(parts[0] in config['volumes'], 'Volume non declare.')
            mounts.append((name, parts[0], parts[1]))
            service['volumes'][service['volumes'].index(mount)] = ':'.join(parts)
        if name != 'apache':
            require(not service.get('ports'), 'Seul Apache doit publier un port.')

    for name in ('php', 'apache'):
        matching = [source for service, source, target in mounts
                    if service == name and target == '/var/www/moodle']
        require(matching == [str(code)], 'Montage du code inattendu : ' + name)
    require(('postgres', 'postgres_data', '/var/lib/postgresql/data') in mounts,
            'Montage PostgreSQL inattendu.')
    require(('php', 'moodledata', '/var/moodledata') in mounts,
            'Montage moodledata inattendu.')
    for name in config['volumes']:
        config['volumes'][name] = {'external': True, 'name': project + '_' + name}
    for name in config['networks']:
        config['networks'][name] = {'external': True, 'name': project + '_' + name}
    for secret in config['secrets'].values():
        require(set(secret) == {'file'}, 'Secret doit etre fourni par fichier.')
        secret['file'] = mapped(secret['file'])
    config['name'] = project
    text = (code / 'config.php').read_text()
    root = re.search(r"\$CFG->wwwroot\s*=\s*'([^']+)'", text)
    require(root is not None and root.group(1) == wwwroot,
            'L URL restauree doit correspondre au pilote pour ce test.')
    require((code / 'vendor/autoload.php').is_file(), 'Dependances Composer absentes.')
    Path(destination).write_text(json.dumps(config, indent=2) + '\n')
    Path(destination).with_suffix('.images').write_text('\n'.join(images) + '\n')
    print('Configuration de reprise : chemins, reseaux et volumes distincts, OK.')


if __name__ == '__main__':
    try:
        require(len(sys.argv) == 9, 'Huit arguments attendus.')
        generate(*sys.argv[1:])
    except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError) as error:
        print('Configuration de reprise refusee : ' + str(error), file=sys.stderr)
        sys.exit(1)
