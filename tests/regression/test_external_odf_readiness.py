"""Run the real ODF recovery block offline with simulated Kubernetes responses.

Requires ansible-playbook and PyYAML. No cluster credentials are used.
Run: python3 -m unittest discover -s tests/regression -v
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "roles/ocp4_workload_external_odf/tasks/workload.yml"
INFO_MODULE = '''
from ansible.module_utils.basic import AnsibleModule
import os
m = AnsibleModule(argument_spec=dict(
    api_version=dict(), kind=dict(), name=dict(), namespace=dict(),
    wait=dict(type='bool', default=False), wait_sleep=dict(type='int'),
    wait_timeout=dict(type='int'), wait_condition=dict(type='dict')))
scenario = os.environ['ODF_TEST_SCENARIO']
if scenario == 'absent' or (scenario == 'recovered' and not m.params['wait']):
    m.exit_json(changed=False, api_found=True, resources=[])
if scenario == 'pending' and m.params['wait']:
    m.fail_json(msg='BackingStore did not become Available before timeout')
if scenario == 'no_conditions':
    m.exit_json(changed=False, api_found=True, resources=[dict(
        kind='BackingStore', metadata=dict(name='noobaa-default-backing-store'))])
m.exit_json(changed=False, api_found=True, resources=[dict(
    apiVersion='noobaa.io/v1alpha1', kind='BackingStore',
    metadata=dict(name='noobaa-default-backing-store'),
    status=dict(conditions=[dict(type='Available', status='True',
                                reason='Backing store mode: OPTIMAL')]))])
'''
K8S_MODULE = '''
from ansible.module_utils.basic import AnsibleModule
m = AnsibleModule(argument_spec=dict(
    state=dict(), api_version=dict(), kind=dict(), namespace=dict(),
    label_selectors=dict(type='list', elements='str')))
m.exit_json(changed=True)
'''


class BackingStoreRecoveryTest(unittest.TestCase):
    def run_scenario(self, scenario):
        tasks = yaml.safe_load(TASKS.read_text())
        start = next(n for n, t in enumerate(tasks)
                     if t['name'] == 'Ensure the default BackingStore becomes available')
        end = next(n for n, t in enumerate(tasks)
                   if t['name'] == 'Change BackingStore numVolumes in pvPool')
        selected = tasks[start:end]

        def shorten(value):
            if isinstance(value, dict):
                for key in ('retries', 'wait_timeout'):
                    if key in value:
                        value[key] = 1
                if 'delay' in value:
                    value['delay'] = 0
                for child in value.values():
                    shorten(child)
            elif isinstance(value, list):
                for child in value:
                    shorten(child)
        shorten(selected)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = root / 'collections/ansible_collections/kubernetes/core/plugins/modules'
            modules.mkdir(parents=True)
            (modules / 'k8s_info.py').write_text(INFO_MODULE)
            (modules / 'k8s.py').write_text(K8S_MODULE)
            play = root / 'test.yml'
            play.write_text(yaml.safe_dump([dict(
                hosts='localhost', gather_facts=False, connection='local',
                vars=dict(ocp4_workload_external_odf_operator_namespace='openshift-storage',
                          ocp4_workload_external_odf_reconcile_operator_selector='name=ocs-operator',
                          ocp4_workload_external_odf_noobaa_deploy_timeout=1),
                tasks=selected)]))
            env = dict(os.environ, ODF_TEST_SCENARIO=scenario,
                       ANSIBLE_COLLECTIONS_PATH=str(root / 'collections'),
                       ANSIBLE_LOCAL_TEMP=str(root / 'ansible-local'),
                       ANSIBLE_REMOTE_TEMP=str(root / 'ansible-remote'),
                       ANSIBLE_NOCOLOR='1')
            return subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play)],
                                  env=env, capture_output=True, text=True, timeout=60)

    def test_absent_after_recovery_is_failure(self):
        result = self.run_scenario('absent')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('default NooBaa BackingStore is missing or unavailable', result.stdout)

    def test_missing_readiness_condition_is_failure(self):
        result = self.run_scenario('no_conditions')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('default NooBaa BackingStore is missing or unavailable', result.stdout)

    def test_ready_is_success(self):
        result = self.run_scenario('ready')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_appears_during_recovery_is_success(self):
        result = self.run_scenario('recovered')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_readiness_timeout_is_failure(self):
        result = self.run_scenario('pending')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('BackingStore did not become Available before timeout', result.stdout)


if __name__ == '__main__':
    unittest.main()
