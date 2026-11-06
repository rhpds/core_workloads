"""Exercise the production HCO retry task offline using a sequenced API stub."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/ocp4_workload_openshift_virtualization'
PREFIX = 'ocp4_workload_openshift_virtualization_'

# Only the API module is replaced. Ansible evaluates the actual production
# register, vars, until, retries, and delay directives without cluster access.
STUB = '''
from ansible.module_utils.basic import AnsibleModule
import json
from pathlib import Path

module = AnsibleModule(argument_spec={
    'api_version': {'type': 'str'}, 'kind': {'type': 'str'},
    'name': {'type': 'str'}, 'namespace': {'type': 'str'},
    'sequence_file': {'type': 'path', 'required': True},
    'counter_file': {'type': 'path', 'required': True},
})
sequence = json.loads(Path(module.params['sequence_file']).read_text())
counter = Path(module.params['counter_file'])
attempt = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(attempt + 1))
result = sequence[min(attempt, len(sequence) - 1)]
if result.get('failed'):
    module.fail_json(**{k: v for k, v in result.items() if k != 'failed'})
module.exit_json(changed=False, **result)
'''


def response(available='True', progressing='False', degraded='False'):
    return {'resources': [{'status': {'conditions': [
        {'type': 'Available', 'status': available},
        {'type': 'Progressing', 'status': progressing},
        {'type': 'Degraded', 'status': degraded},
    ]}}]}


class HyperConvergedReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tasks = yaml.safe_load((ROLE / 'tasks/workload.yml').read_text())
        block = next(t['block'] for t in tasks if t['name'] == 'Deploy and configure HyperConverged')
        cls.task = next(t for t in block if t['name'] == 'Wait until HyperConverged is installed')
        cls.defaults = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())

    def run_sequence(self, sequence, success, attempts, retries=2):
        with tempfile.TemporaryDirectory(prefix='hco-readiness-test-') as directory:
            tmp = Path(directory)
            library = tmp / 'library'
            library.mkdir()
            (library / 'hco_info.py').write_text(STUB)
            (tmp / 'sequence.json').write_text(json.dumps(sequence))
            task = copy.deepcopy(self.task)
            task['hco_info'] = task.pop('kubernetes.core.k8s_info')
            task['hco_info'].update(sequence_file=str(tmp / 'sequence.json'),
                                    counter_file=str(tmp / 'counter'))
            variables = dict(self.defaults)
            variables.update({PREFIX + 'hco_wait_retries': retries,
                              PREFIX + 'hco_wait_delay': 0,
                              'ansible_python_interpreter': sys.executable})
            play = [dict(hosts='localhost', connection='local', gather_facts=False,
                         vars=variables, tasks=[task])]
            playbook = tmp / 'playbook.yml'
            playbook.write_text(yaml.safe_dump(play, sort_keys=False))
            environment = dict(os.environ, ANSIBLE_LIBRARY=str(library),
                               ANSIBLE_LOCAL_TEMP=str(tmp / 'local'),
                               ANSIBLE_REMOTE_TEMP=str(tmp / 'remote'), ANSIBLE_NOCOLOR='1')
            result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook',
                                     '-i', 'localhost,', str(playbook)], cwd=tmp,
                                    env=environment, capture_output=True, text=True, timeout=60)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode == 0, success, output)
            self.assertEqual(int((tmp / 'counter').read_text()), attempts, output)
            self.assertNotIn('Error while evaluating conditional', output)

    def test_recovery_after_api_error_and_incomplete_conditions(self):
        sequence = [
            {'failed': True, 'msg': '500 conversion webhook: connection refused'},
            {}, {'resources': []}, {'resources': [{}]},
            {'resources': [{'status': {}}]},
            {'resources': [{'status': {'conditions': []}}]},
            response(available='False'), response(progressing='True'),
            response(degraded='True'), response(available='Unknown'), response(),
        ]
        self.run_sequence(sequence, success=True, attempts=len(sequence), retries=len(sequence))

    def test_unhealthy_or_failed_queries_exhaust_retry_budget(self):
        missing_condition = response()
        missing_condition['resources'][0]['status']['conditions'].pop()
        failed_with_stale_resource = response()
        failed_with_stale_resource.update(failed=True, msg='API request failed')
        for result in [
            {'failed': True, 'msg': '500 webhook unavailable'}, {}, {'resources': []},
            {'resources': [{}]}, missing_condition, failed_with_stale_resource,
            response(available='False'), response(progressing='True'),
            response(degraded='True'), response(available='Unknown'),
        ]:
            with self.subTest(result=result):
                self.run_sequence([result], success=False, attempts=3)

    def test_ready_resource_succeeds_without_retry(self):
        self.run_sequence([response()], success=True, attempts=1)

    def test_default_retry_budget(self):
        self.assertEqual(self.defaults[PREFIX + 'hco_wait_retries'], 120)
        self.assertEqual(self.defaults[PREFIX + 'hco_wait_delay'], 10)
        self.assertNotIn('wait', self.task['kubernetes.core.k8s_info'])


if __name__ == '__main__':
    unittest.main()
