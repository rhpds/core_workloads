"""Offline Ansible/template behavior; no Kubernetes or Azure operations."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/ocp4_workload_cert_manager'
PREFIX = 'ocp4_workload_cert_manager_'
AZ = PREFIX + 'azure_'
SECRET = 'synthetic: "quoted" # secret\nsecond line'


class AzureCertManagerTests(unittest.TestCase):
    def test_issuer_templates_preserve_other_solvers(self):
        defaults = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(ROLE / 'templates'),
                                 undefined=jinja2.StrictUndefined)
        env.filters.update(bool=bool, to_json=json.dumps)
        for provider, solver in [('aws', 'route53'), ('gcp', 'cloudDNS'), ('azure', 'azureDNS')]:
            for fallback in (False, True):
                with self.subTest(provider=provider, fallback=fallback):
                    values = dict(defaults)
                    values.update({PREFIX + 'cloud_provider': provider,
                                   PREFIX + 'provider_fallback': 'letsencrypt',
                                   PREFIX + 'acme_url_fallback': 'https://example.test/acme',
                                   PREFIX + 'aws_region': 'us-east-1', PREFIX + 'aws_access_key_id': 'fake',
                                   PREFIX + 'gcp_project_id': 'fake-project', 'guid': 'test',
                                   '_ocp4_workload_cert_manager_hostedzoneid': 'fake-zone',
                                   '_ocp4_workload_cert_manager_api_hostname': 'api.test.example.com',
                                   '_ocp4_workload_cert_manager_wildcard_domain': 'apps.test.example.com'})
                    values.update({AZ + key: value for key, value in dict(
                        client_id='fake-app', environment='AzureUSGovernmentCloud',
                        hostedzone_name='example.com', resource_group_name='dns',
                        subscription_id='fake-sub', tenant_id='fake-tenant').items()})
                    filename = 'clusterissuer-fallback.yaml.j2' if fallback else 'clusterissuer.yaml.j2'
                    document = yaml.safe_load(env.get_template(filename).render(values))
                    dns = document['spec']['acme']['solvers'][0]['dns01']
                    self.assertEqual(list(dns), [solver])
                    if provider == 'azure':
                        self.assertEqual(dns[solver]['environment'], 'AzureUSGovernmentCloud')
                        self.assertEqual(dns[solver]['clientSecretSecretRef']['key'], 'client-secret')

    def test_real_ansible_defaults_validation_and_certificate_selection(self):
        """Execute production task conditions, replacing external writes with local files."""
        with tempfile.TemporaryDirectory(prefix='cert-manager-test-') as directory:
            tmp = Path(directory)
            target = tmp / 'collections/ansible_collections/agnosticd/core_workloads/roles/ocp4_workload_cert_manager'
            shutil.copytree(ROLE, target)
            tasks = yaml.safe_load((ROLE / 'tasks/workload.yml').read_text())
            selected = []
            for task in tasks:
                if task['name'] == 'Install Ingress controller certificate':
                    break
                if task['name'] == 'Install Red Hat Cert Manager operator':
                    selected.append({'name': task['name'], 'ansible.builtin.copy': {
                        'dest': '{{ test_output }}/operator', 'content': 'installed', 'mode': 'u=rw,go='}})
                elif 'kubernetes.core.k8s' in task:
                    template = task['kubernetes.core.k8s']['template']
                    replacement = {k: v for k, v in task.items() if k != 'kubernetes.core.k8s'}
                    replacement['ansible.builtin.copy'] = {
                        'dest': '{{ test_output }}/' + template.replace('.j2', ''),
                        'content': "{{ lookup('ansible.builtin.template', '" + template + "') }}",
                        'mode': 'u=rw,go='}
                    replacement.update(retries=1, delay=0)
                    selected.append(replacement)
                elif 'ansible.builtin.include_tasks' in task:
                    selected.append(task)
                elif 'ansible.builtin.set_fact' in task:
                    selected.append(task)
            (target / 'tasks/workload.yml').write_text(yaml.safe_dump(selected, sort_keys=False))
            secret = yaml.safe_load((ROLE / 'tasks/cert_manager_azure.yml').read_text())[0]
            self.assertTrue(secret['no_log'])
            secret.pop('kubernetes.core.k8s')
            secret['ansible.builtin.copy'] = {
                'dest': '{{ test_output }}/secret.yaml',
                'content': "{{ lookup('ansible.builtin.template', 'secret-azure-credentials.yaml.j2') }}",
                'mode': 'u=rw,go='}
            (target / 'tasks/cert_manager_azure.yml').write_text(yaml.safe_dump([secret], sort_keys=False))
            playbook = tmp / 'playbook.yml'
            playbook.write_text(yaml.safe_dump([dict(hosts='bastion', connection='local', gather_facts=False,
                roles=['agnosticd.core_workloads.ocp4_workload_cert_manager'])], sort_keys=False))
            canonical = dict(azure_service_principal_id='app', azure_service_principal_password=SECRET,
                             azure_subscription='subscription', azure_tenant='tenant',
                             azure_resource_group='dns', azure_dns_zone='example.com')
            explicit = {AZ + key: value for key, value in dict(client_id='app', client_secret=SECRET,
                         subscription_id='subscription', tenant_id='tenant', resource_group_name='dns',
                         hostedzone_name='example.com').items()}
            normalized = dict(azure_auth=dict(app_id='app', password=SECRET, subscription_id='subscription', tenant='tenant'),
                              azure_settings=dict(dns_zone='example.com', sandbox_group='dns'))
            v2_config = ROOT.parents[2] / 'agnosticd-v2/ansible/configs/openshift-cluster/azure/default_vars.yml'
            config_mapping = {key: value for key, value in yaml.safe_load(v2_config.read_text()).items()
                              if key.startswith(AZ)} if v2_config.exists() else {}
            if v2_config.exists():
                self.assertEqual(len(config_mapping), 6)
            cases = [('canonical', canonical, True),
                     ('explicit-overrides', dict(canonical, **explicit, azure_dns_zone='wrong.test'), True),
                     ('missing-secret', dict(canonical, azure_service_principal_password=''), False),
                     ('missing-client', dict(canonical, azure_service_principal_id=''), False),
                     ('outside-zone', dict(canonical, openshift_cluster_ingress_domain='apps.aroapp.io'), False),
                     ('suffix-boundary', dict(canonical, openshift_cluster_ingress_domain='notexample.com'), False),
                     ('case-trailing-dot', dict(canonical, azure_dns_zone='EXAMPLE.COM.'), True)]
            if config_mapping:
                cases.append(('controller-to-bastion', config_mapping, True))
            for ingress, api in ((True, False), (False, True), (True, True), (False, False)):
                cases.append(('selection-' + str(ingress) + '-' + str(api), dict(canonical, **{
                    PREFIX + 'install_ingress_certificates': ingress, PREFIX + 'install_api_certificates': api}), True))
            for label, overrides, success in cases:
                with self.subTest(case=label):
                    output = tmp / label
                    output.mkdir()
                    variables = dict(ACTION='provision', cloud_provider='azure', test_output=str(output),
                                     openshift_api_url='https://api.test.example.com:6443',
                                     openshift_cluster_ingress_domain='apps.test.example.com',
                                     ansible_python_interpreter=sys.executable)
                    variables.update({PREFIX + 'provider_fallback': 'letsencrypt',
                                      PREFIX + 'acme_url_fallback': 'https://example.test/acme'})
                    variables.update(overrides)
                    # Disabled endpoints need no hostname input, even if outside the zone.
                    if variables.get(PREFIX + 'install_api_certificates') is False:
                        variables.pop('openshift_api_url')
                    if variables.get(PREFIX + 'install_ingress_certificates') is False:
                        variables.pop('openshift_cluster_ingress_domain')
                    inventory = tmp / 'inventory.json'
                    inventory.write_text(json.dumps({'all': {'hosts': {
                        'localhost': normalized, 'bastion': variables}}}))
                    environment = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=str(tmp / 'collections'),
                        ANSIBLE_LOCAL_TEMP=str(tmp / 'local'), ANSIBLE_REMOTE_TEMP=str(tmp / 'remote'))
                    result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook',
                        '-i', str(inventory), str(playbook)], env=environment, text=True,
                        capture_output=True, timeout=60, check=False)
                    self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                    self.assertNotIn('synthetic:', result.stdout + result.stderr)
                    self.assertEqual((output / 'operator').exists(), success)
                    if not success:
                        self.assertFalse((output / 'secret.yaml').exists())
                        continue
                    credentials = yaml.safe_load((output / 'secret.yaml').read_text())
                    self.assertEqual(credentials['stringData']['client-secret'], SECRET)
                    for kind, enabled in [('ingress', variables.get(PREFIX + 'install_ingress_certificates', True)),
                                          ('api', variables.get(PREFIX + 'install_api_certificates', False))]:
                        self.assertEqual((output / ('certificate-' + kind + '.yaml')).exists(), enabled)
                    for name in ['clusterissuer.yaml', 'clusterissuer-fallback.yaml']:
                        issuer = yaml.safe_load((output / name).read_text())
                        solver = issuer['spec']['acme']['solvers'][0]['dns01']['azureDNS']
                        self.assertEqual(solver['clientID'], 'app')
                        self.assertEqual(solver['subscriptionID'], 'subscription')
                        self.assertEqual(solver['clientSecretSecretRef']['name'], 'cert-manager-azure-creds')


if __name__ == '__main__':
    unittest.main()
