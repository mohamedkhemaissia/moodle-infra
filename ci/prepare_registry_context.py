"""Modifier uniquement les FROM des copies temporaires de construction CI."""
import argparse
import json
from pathlib import Path
import re
import shutil

from registry_sources import image_reference

ROOT = Path(__file__).resolve().parents[1]


def prepare_context(component, destination, *, root=ROOT, source=None):
    destination = Path(destination).resolve()
    root = Path(root).resolve()
    if destination.is_relative_to(root):
        raise ValueError('Le contexte de construction doit rester hors du depot.')
    if component == 'dashboard':
        if destination.exists():
            raise ValueError('Le contexte dashboard doit etre un nouveau repertoire.')
        shutil.copytree(root / 'dashboard', destination,
                        ignore=shutil.ignore_patterns('node_modules', 'dist', '__pycache__'))
    elif component == 'api':
        if not destination.is_dir():
            raise ValueError('Assembler le contexte API partage avant cette etape.')
    else:
        raise ValueError('Composant de construction inconnu.')
    dockerfile = destination / 'Dockerfile'
    if dockerfile.is_symlink() or not dockerfile.is_file():
        raise ValueError('Dockerfile du contexte absent ou incompatible.')
    before = dockerfile.read_text()
    references = []

    def replace(match):
        original = match.group('image')
        alternate = image_reference(original, source=source)
        references.append({'original': original, 'alternate': alternate})
        return match.group('prefix') + alternate + match.group('suffix')

    after = re.sub(r'^(?P<prefix>FROM\s+)(?P<image>\S+)(?P<suffix>[^\n]*)$',
                   replace, before, flags=re.MULTILINE)
    if not references:
        raise ValueError('Image de base du Dockerfile absente.')
    dockerfile.write_text(after)
    return {'status': 'prepared', 'component': component,
            'context_directory': str(destination), 'base_images': references}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=['api', 'dashboard'])
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare_context(args.component, args.output_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, 'Preparation CI arretee : ' + str(exc) + '\n')
    print(json.dumps(result, indent=2))
