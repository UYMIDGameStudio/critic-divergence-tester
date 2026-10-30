"""CI partitions must cover discovery and preserve failures rather than hide them."""
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.run_test_shard import ROOT, flatten_tests, select_shard


class CIShardingTests(unittest.TestCase):
    def test_every_entry_including_duplicate_ids_is_assigned_exactly_once(self):
        tests = [unittest.FunctionTestCase(lambda:None) for _ in range(11)]
        nested = unittest.TestSuite([unittest.TestSuite(tests[:4]), unittest.TestSuite(tests[4:])])
        self.assertEqual(list(flatten_tests(nested)), tests)
        for count in (1,2,3,5):
            partitions = [select_shard(tests,index,count) for index in range(count)]
            self.assertEqual(Counter(id(test) for shard in partitions for test in shard), Counter(map(id,tests)))
            self.assertLessEqual(max(map(len,partitions))-min(map(len,partitions)),1)
        for index,count in ((-1,2),(2,2),(0,0),(True,2)):
            with self.subTest(index=index,count=count), self.assertRaises(ValueError):
                select_shard(tests,index,count)

    def test_discovery_import_error_remains_a_failing_test(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'test_broken.py').write_text('raise RuntimeError("owned import failure")\n',encoding='utf-8')
            result=subprocess.run([sys.executable,str(ROOT/'scripts/run_test_shard.py'),'--start-directory',str(root)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,1)
            self.assertIn('owned import failure',result.stderr)

    def test_actual_discovery_manifests_partition_skipped_and_failed_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'test_owned.py').write_text('import unittest\nclass Owned(unittest.TestCase):\n def test_a(self): pass\n @unittest.skip("owned skip")\n def test_b(self): pass\n def test_c(self): self.fail("owned failure")\n',encoding='utf-8')
            reports=[]
            for index in (0,1):
                manifest=root/f'shard-{index}.json'
                result=subprocess.run([sys.executable,str(ROOT/'scripts/run_test_shard.py'),'--start-directory',str(root),'--shard-count','2','--shard-index',str(index),'--manifest',str(manifest)],capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,1 if index==0 else 0)
                reports.append(json.loads(manifest.read_text(encoding='utf-8')))
            self.assertEqual(reports[0]['discovery_sha256'],reports[1]['discovery_sha256'])
            self.assertEqual(set(reports[0]['test_ids']) & set(reports[1]['test_ids']),set())
            self.assertEqual(sum(report['selected_count'] for report in reports),3)
            self.assertTrue(any(name.endswith('test_b') for report in reports for name in report['test_ids']))

    def test_empty_selection_cannot_look_like_a_successful_test_job(self):
        with tempfile.TemporaryDirectory() as temp:
            result=subprocess.run([sys.executable,str(ROOT/'scripts/run_test_shard.py'),'--start-directory',temp],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,2)
            self.assertIn('No tests selected',result.stderr)


if __name__ == '__main__':
    unittest.main()
