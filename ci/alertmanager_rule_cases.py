"""Scenarios independants : panne confirmee, collecte inconnue, temporisation et reprise."""


def rule_cases(alert_names):
    normal = {f'up{{job="{job}"}}': '1+0x60' for job in (
        'linux', 'blackbox_exporter', 'moodle_https', 'alertmanager', 'moodle_phpfpm', 'moodle_postgres')}
    normal.update({
        'probe_success{job="moodle_https"}': '1+0x60',
        'phpfpm_up{job="moodle_phpfpm"}': '1+0x60',
        'pg_up{job="moodle_postgres"}': '1+0x60',
        'phpfpm_active_processes{job="moodle_phpfpm",pool="www"}': '4+0x60',
        'phpfpm_idle_processes{job="moodle_phpfpm",pool="www"}': '1+0x60',
        'phpfpm_listen_queue{job="moodle_phpfpm",pool="www"}': '0+0x60',
        'node_cpu_seconds_total{job="linux",mode="idle",cpu="0"}': '0+12x60',
        'node_memory_MemAvailable_bytes{job="linux"}': '600+0x60',
        'node_memory_MemTotal_bytes{job="linux"}': '1000+0x60',
        'node_filesystem_avail_bytes{job="linux",mountpoint="/",fstype="ext4"}': '300+0x60',
        'node_filesystem_size_bytes{job="linux",mountpoint="/",fstype="ext4"}': '1000+0x60',
        'moodle_container_collection_success{job="linux"}': '1+0x60',
        'moodle_container_inventory_timestamp_seconds{job="linux"}': '0+15x60',
        'node_textfile_scrape_error{job="linux"}': '0+0x60',
    })

    def container(metric, service='php', project='moodle-pilote'):
        return f'moodle_container_{metric}{{job="linux",project="{project}",compose_service="{service}"}}'

    for service in ('apache', 'php', 'postgres'):
        for metric in ('instances', 'running'):
            normal[container(metric, service)] = '1+0x60'

    cases = []

    def case(name, changes=None, expected=(), when='6m'):
        samples = {**normal, **(changes or {})}
        cases.append({'name': name, 'interval': '15s',
                      'input_series': [{'series': series, 'values': value}
                                       for series, value in samples.items() if value is not None],
                      'promql_expr_test': [
                          {'expr': f'count(ALERTS{{alertname="{alert}",alertstate="firing",scope="moodle-pilote"}}) or vector(0)',
                           'eval_time': when, 'exp_samples': [{'labels': '{}', 'value': expected.count(alert)}]}
                          for alert in alert_names]})

    case('Etat normal : aucune alerte')
    case('Echec HTTPS bref : attendre la confirmation',
         {'probe_success{job="moodle_https"}': '0+0x2 1+0x60'}, when='45s')
    case('HTTPS confirme indisponible', {'probe_success{job="moodle_https"}': '0+0x60'},
         ('MoodleHTTPSUnavailable',), '2m')
    case('Reprise HTTPS : resolution', {'probe_success{job="moodle_https"}': '0+0x8 1+0x60'}, when='4m')
    case('Collecte HTTPS perdue : service inconnu',
         {'up{job="moodle_https"}': '0+0x60', 'probe_success{job="moodle_https"}': '0+0x60'},
         ('MoodleHTTPSCollectionUnavailable',))
    case('Sonde absente malgre un scrape reussi', {'probe_success{job="moodle_https"}': None},
         ('MoodleHTTPSCollectionUnavailable',))
    case('Ancienne sonde en echec : service inconnu', {'probe_success{job="moodle_https"}': '0+0x4'},
         ('MoodleHTTPSCollectionUnavailable',), '4m')
    case('CPU eleve persistant', {'node_cpu_seconds_total{job="linux",mode="idle",cpu="0"}': '0+1.5x60'},
         ('MoodleCPUHigh',))
    case('Memoire et disque eleves persistants',
         {'node_memory_MemAvailable_bytes{job="linux"}': '50+0x60',
          'node_filesystem_avail_bytes{job="linux",mountpoint="/",fstype="ext4"}': '50+0x60'},
         ('MoodleMemoryHigh', 'MoodleDiskLow'))
    case('Collecte Linux perdue : ne pas conclure a une saturation',
         {'up{job="linux"}': '0+0x60', 'node_memory_MemAvailable_bytes{job="linux"}': '50+0x60',
          container('running'): '0+0x60'}, ('MoodleLinuxCollectionUnavailable',))
    case('Scrape Linux ancien : bloquer les diagnostics de ressource',
         {'up{job="linux"}': '1+0x4', 'node_memory_MemAvailable_bytes{job="linux"}': '50+0x60'},
         ('MoodleLinuxCollectionUnavailable',), '4m')
    case('Mesure de ressource manquante', {'node_memory_MemAvailable_bytes{job="linux"}': None},
         ('MoodleHostMetricsUnavailable',))
    case('PHP inaccessible avec exporteur sain', {'phpfpm_up{job="moodle_phpfpm"}': '0+0x60'},
         ('MoodlePHPUnavailable',))
    case('PostgreSQL inaccessible avec exporteur sain', {'pg_up{job="moodle_postgres"}': '0+0x60'},
         ('MoodlePostgresUnavailable',))
    case('Exporteur PostgreSQL arrete : service inconnu',
         {'up{job="moodle_postgres"}': '0+0x60', 'pg_up{job="moodle_postgres"}': '0+0x60'},
         ('MoodlePostgresCollectionUnavailable',))
    case('File PHP persistante sans processus libre',
         {'phpfpm_listen_queue{job="moodle_phpfpm",pool="www"}': '2+0x60',
          'phpfpm_idle_processes{job="moodle_phpfpm",pool="www"}': '0+0x60'},
         ('MoodlePHPPoolQueue',))
    case('Conteneur PHP confirme arrete', {container('running'): '0+0x60'},
         ('MoodleContainerUnavailable',))
    case('Conteneur Apache absent', {container('instances', 'apache'): '0+0x60',
                                     container('running', 'apache'): None}, ('MoodleContainerUnavailable',))
    case('Conteneur PHP duplique', {container('instances'): '2+0x60'}, ('MoodleContainerUnavailable',))
    case('Inventaire ancien : aucun arret deduit',
         {'moodle_container_inventory_timestamp_seconds{job="linux"}': '0+0x60',
          container('running'): '0+0x60'}, ('MoodleContainerCollectionUnavailable',))
    case('Horloge incoherente : inventaire inconnu',
         {'moodle_container_inventory_timestamp_seconds{job="linux"}': '3600+0x60',
          container('running'): '0+0x60'}, ('MoodleContainerCollectionUnavailable',))
    case('Echec Docker : anciennes ressources abandonnees',
         {'moodle_container_collection_success{job="linux"}': '0+0x60',
          'moodle_container_inventory_timestamp_seconds{job="linux"}': None,
          container('running'): '0+0x60'}, ('MoodleContainerCollectionUnavailable',))
    case('Erreur de parsing textfile', {'node_textfile_scrape_error{job="linux"}': '1+0x60',
                                       container('running'): '0+0x60'}, ('MoodleContainerCollectionUnavailable',))
    case('Service attendu sans serie : inventaire incomplet', {container('instances'): None},
         ('MoodleContainerCollectionUnavailable',))
    case('Ignorer les anciens projets de restauration',
         {container('instances', project='moodle-restauration-test'): '0+0x60',
          container('running', project='moodle-restauration-test'): '0+0x60'})
    case('Alertmanager indisponible : signal visible dans Prometheus', {'up{job="alertmanager"}': '0+0x60'},
         ('MoodleAlertmanagerCollectionUnavailable',))
    return {'rule_files': ['rules.yml'], 'evaluation_interval': '15s', 'tests': cases}
