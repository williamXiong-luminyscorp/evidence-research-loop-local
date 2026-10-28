"""Actual MCP function/transport checks; known fixtures, not autonomous search."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
from mcp.client import Client


class CompositionMCPTests(unittest.TestCase):
    def setUp(self):
        fixture_base = Path(os.environ.get('ERL_TEST_TMP', ROOT / '.erl-test-tmp'))
        self.root = fixture_base / ('mcp-' + uuid.uuid4().hex[:8])
        self.root.mkdir(parents=True)
        self.patches = [patch.object(server, 'PROJECTS_ROOT', self.root),
                        patch.object(server, 'LOCAL_REGISTRY', self.root/'.local-projects.json')]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_conditional_then_bound_acceptance_and_revoke(self):
        async def run():
            server.create_project('composition-test', 'composition-graph')
            initialized = await server.initialize_composition_graph('composition-test')
            self.assertEqual(initialized['exit_code'], 0, initialized)
            self.assertFalse(initialized['result']['accepted'])
            with self.assertRaises(RuntimeError):
                server.write_files('composition-test', [{'path':'verifier_adapter.py','content':'changed'}], overwrite=True)
            reduced = await server.submit_composition('composition-test',
                {'kind':'reduction','claim_id':'pipeline','premises':['scale','offset']},
                artifact_content='{"order":["scale","offset"]}')
            self.assertEqual(reduced['result']['outcome']['status'], 'conditionally_validated', reduced)
            self.assertFalse(reduced['result']['graph']['accepted'])
            bindings = {}
            for cid, content in [('scale','{"operation":"scale","factor":2}'),
                                 ('offset','{"operation":"offset","amount":3}')]:
                result = await server.submit_composition('composition-test', {'kind':'direct','claim_id':cid},
                                                        artifact_content=content)
                self.assertEqual(result['exit_code'], 0, result)
                bindings[cid] = result['result']['evidence_id']
            validated = await server.submit_composition('composition-test',
                {'kind':'validate','reduction_id':reduced['result']['evidence_id'],'bindings':bindings})
            self.assertTrue(validated['result']['graph']['accepted'], validated)
            audited = await server.audit_composition_graph('composition-test')
            self.assertTrue(audited['result']['valid'] and audited['result']['accepted'], audited)
            before = audited['result']['event_count']
            for _ in range(2):
                resumed = await server.resume_composition_graph('composition-test')
                self.assertEqual(resumed['result']['event_count'], before)
            revoked = await server.revoke_composition_evidence('composition-test', bindings['scale'], 'explicit regression retraction')
            self.assertFalse(revoked['result']['accepted'])
            self.assertTrue(revoked['result']['valid'])
        asyncio.run(run())

    def test_transfer_paths_cannot_escape(self):
        async def run():
            server.create_project('path-test', 'composition-graph')
            with self.assertRaises(ValueError):
                await server.initialize_composition_graph('path-test', '../outside.json')
            with self.assertRaises(ValueError):
                await server.submit_composition('path-test', {'kind':'direct','claim_id':'scale','artifact':'../../outside'})
            with patch.object(server, '_allowed_local_roots', return_value=[]):
                with self.assertRaises(PermissionError):
                    await server.import_composition_asset('path-test', 'scale', json.dumps({'source_project':str(self.root.parent/'outside')}))
        asyncio.run(run())

    def test_mcp_transport_exposes_unambiguous_capabilities(self):
        async def run():
            async with Client(server.mcp) as client:
                names = {t.name for t in (await client.list_tools()).tools}
                self.assertIn('submit_composition', names)
                self.assertIn('audit_composition_graph', names)
                result = await client.call_tool('get_capabilities', {})
                self.assertEqual(result.structured_content['execution']['composition_guarantee'], 'empirical')
                self.assertTrue(result.structured_content['engine_hash_valid'])
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main(verbosity=2)
