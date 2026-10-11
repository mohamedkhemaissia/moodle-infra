import unittest

from alertmanager_config import render, values


class AlertmanagerConfigurationTests(unittest.TestCase):
    def test_service_is_local_bounded_and_keeps_silences_on_persistent_storage(self):
        document = render('compose')
        service = document['services']['alertmanager']
        self.assertIn('--web.listen-address=127.0.0.1:9093', service['command'])
        self.assertIn('--cluster.listen-address=', service['command'])
        self.assertEqual(service['user'], '65534:65534')
        self.assertTrue(service['read_only'])
        self.assertEqual(service['mem_limit'], '96m')
        self.assertIn('alertmanager_data:/alertmanager', service['volumes'])
        self.assertNotIn('docker.sock', str(service))
        self.assertEqual(set(document['services']), {'prometheus', 'node-exporter', 'blackbox', 'alertmanager'})

    def test_disabled_feature_preserves_the_previous_monitoring_stack(self):
        parameters = values(monitoring_alertmanager_enabled=False)
        self.assertNotIn('alertmanager', render('compose', parameters)['services'])
        self.assertNotIn('alertmanager_data', render('compose', parameters)['volumes'])
        prometheus = render('prometheus', parameters)
        self.assertNotIn('alerting', prometheus)
        self.assertNotIn('rule_files', prometheus)

    def test_default_has_no_outbound_receiver_and_email_uses_a_secret_file_with_tls(self):
        default = render('alertmanager')
        self.assertEqual(default['route']['receiver'], 'local')
        self.assertEqual(default['receivers'], [{'name': 'local'}])
        parameters = values(monitoring_alert_email_enabled=True,
                            monitoring_alert_smtp_smarthost='smtp.example.test:587',
                            monitoring_alert_smtp_from='moodle@example.test',
                            monitoring_alert_email_to='operator@example.test',
                            monitoring_alert_smtp_username='moodle@example.test')
        enabled = render('alertmanager', parameters)
        self.assertEqual(enabled['route']['receiver'], 'moodle-email')
        self.assertTrue(enabled['global']['smtp_require_tls'])
        self.assertEqual(enabled['global']['smtp_auth_password_file'], '/run/secrets/smtp-password')
        self.assertNotIn('smtp_auth_password', enabled['global'])
        self.assertTrue(enabled['receivers'][1]['email_configs'][0]['send_resolved'])
        self.assertIn('/etc/moodle-alertmanager/smtp-password:/run/secrets/smtp-password:ro',
                      render('compose', parameters)['services']['alertmanager']['volumes'])

    def test_optional_rules_follow_the_exporters_enabled_in_the_inventory(self):
        parameters = values(monitoring_application_metrics_enabled=False, monitoring_container_metrics_enabled=False)
        names = {rule['alert'] for group in render('alerts', parameters)['groups'] for rule in group['rules']}
        self.assertNotIn('MoodlePHPUnavailable', names)
        self.assertNotIn('MoodleContainerUnavailable', names)
        self.assertIn('MoodleHTTPSUnavailable', names)


if __name__ == '__main__':
    unittest.main()
