"""Sources alternatives d'images reservees aux jobs CI, verrouillees par empreinte."""
from functools import lru_cache
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'ecr-public'


@lru_cache(maxsize=1)
def locked_images():
    document = json.loads((ROOT / 'ci/registry-images.json').read_text())
    if document.get('format') != 'moodle-ci-registry-images-v1':
        raise ValueError('Format des sources CI invalide.')
    images = document['images']
    for original, alternate in images.items():
        if not isinstance(original, str) or not isinstance(alternate, str):
            raise ValueError('Reference CI invalide.')
        if not re.fullmatch(r'(?:public\.ecr\.aws/docker/library/[a-z]+|quay\.io/prometheus/prometheus):[a-zA-Z0-9._-]+@sha256:[0-9a-f]{64}', alternate):
            raise ValueError('Source publique CI non autorisee.')
        if '@sha256:' in original and original.split('@', 1)[1] != alternate.split('@', 1)[1]:
            raise ValueError('L empreinte de l image originale doit etre conservee.')
    return images


def image_reference(reference, *, source=None):
    source = source or os.getenv('CI_IMAGE_SOURCE', 'dockerhub')
    if source == 'dockerhub':
        return reference
    if source != SOURCE:
        raise ValueError('Source CI inconnue.')
    mapped = locked_images().get(reference)
    if mapped:
        return mapped
    if reference.startswith(('ghcr.io/', 'quay.io/', 'moodle-pfe/')):
        return reference
    raise ValueError('Image CI absente du verrouillage : ' + reference)


def docker_arguments(arguments):
    """Garder les commandes natives ; substituer seulement les images connues."""
    if not arguments or arguments[0] not in ('docker', '/usr/bin/docker'):
        return arguments
    if os.getenv('CI_IMAGE_SOURCE', 'dockerhub') == 'dockerhub':
        return arguments
    known = locked_images()
    return [image_reference(value) if value in known else value for value in arguments]
