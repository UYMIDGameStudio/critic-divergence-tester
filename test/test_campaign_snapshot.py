"""A long campaign must review one fixed corpus even if files change."""
import argparse
import importlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from critic_execution import ExecutorResult
from test.test_critic_runner import VALID_REPORT

campaign_module = importlib.import_module('cli.campaign')
run_module = importlib.import_module('cli.run')


class CampaignSnapshotTests(unittest.TestCase):
    def test_source_and_protocol_are_frozen_before_first_executor_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'draft.md'
            source.write_bytes(b'Original manuscript')
            snapshots = {
                name: (f'Original {name}', f'Original {name}'.encode())
                for name in ('critic-individualist', 'critic-contrastivist')
            }
            args = argparse.Namespace(
                manuscript=str(source), protocol=None, repeat=2,
                campaigns_dir=str(root / 'campaigns'), allow_test_artifact=False,
                timeout=5, max_output_bytes=1024 * 1024, order_seed='snapshot',
                executor=['fixture'], executor_label='fixture',
            )
            prompts = []

            def execute(command, prompt, **kwargs):
                prompts.append(prompt)
                source.unlink(missing_ok=True)
                return ExecutorResult(0, VALID_REPORT.encode(), b'')

            output, progress = io.StringIO(), io.StringIO()
            with patch.object(campaign_module, 'load_protocol', side_effect=lambda name, _: snapshots[name]):
                with patch.object(run_module, 'load_protocol', side_effect=AssertionError('protocol reread')):
                    with patch.object(run_module, 'execute_with_limits', side_effect=execute):
                        with redirect_stdout(output), redirect_stderr(progress):
                            self.assertEqual(campaign_module.campaign(args), 0)
            self.assertEqual(len(prompts), 4)
            self.assertTrue(all(b'Original manuscript' in prompt for prompt in prompts))
            campaign_dir = Path(output.getvalue().strip())
            manifest = json.loads((campaign_dir / 'campaign.json').read_text())
            for record in manifest['runs']:
                child = json.loads((campaign_dir / record['run_dir'] / 'manifest.json').read_text())
                self.assertEqual(child['source_sha256'], manifest['source_sha256'])
                self.assertEqual(child['status'], 'succeeded')
                self.assertEqual(child['protocol_sha256'], campaign_module.sha256_bytes(snapshots[child['protocol']][1]))
            self.assertIn('[4/4]', progress.getvalue())
            self.assertEqual(progress.getvalue().count(': succeeded'), 4)


if __name__ == '__main__':
    unittest.main()
