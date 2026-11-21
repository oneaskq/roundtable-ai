import json
from dataclasses import replace
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from .catalog import IMPLEMENTED, chat_available
from .connections import (ConnectionError, ProviderConnection, load_connection,
                          save_connection, save_named_connection)
from .models import CapabilityEvidence
from .team_config import (ROLES, load_routes, resolve_team, save_routes,
                          team_revision, team_summary)


class TeamConfigurationTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        override = self.settings(PROVIDER_CONNECTION_PATH=self.path / 'active.json',
                                 NAMED_CONNECTIONS_DIR=self.path / 'named',
                                 TEAM_ROUTING_PATH=self.path / 'team.json',
                                 TEAM_ROLE_CONNECTIONS=None)
        override.enable()
        self.addCleanup(override.disable)
        self.deepseek = ProviderConnection('deepseek', 'synthetic-first', 'synthetic-first-key', 'test')
        self.kimi = ProviderConnection('kimi', 'synthetic-second', 'synthetic-second-key', 'test')
        save_connection(self.deepseek)
        save_named_connection(self.kimi)

    def approve(self, connection):
        for capability in IMPLEMENTED:
            CapabilityEvidence.objects.create(
                capability=capability, connection_fingerprint=connection.fingerprint,
                provider=connection.provider, model=connection.model, service_region=connection.service_region,
                verified_at=timezone.now(), expires_at=timezone.now() + timedelta(days=1),
                evidence='Synthetic local test only', enabled=True, region_approved=True,
                compliance_approved=True)

    def test_default_team_uses_active_connection_for_every_role(self):
        connections = resolve_team()
        self.assertEqual(set(connections), {role for role, _ in ROLES})
        self.assertTrue(all(connection == self.deepseek for connection in connections.values()))

    def test_configured_named_roles_never_change_active_connection(self):
        save_routes({'challenger': 'kimi', 'reviewer': 'kimi'})
        connections = resolve_team()
        self.assertEqual(connections['challenger'], self.kimi)
        self.assertEqual(connections['synthesizer'], self.deepseek)
        self.assertEqual(load_connection(), self.deepseek)
        text = (self.path / 'team.json').read_text(encoding='utf-8')
        self.assertNotIn(self.kimi.api_key, text)
        self.assertNotIn(self.deepseek.api_key, text)

    def test_missing_named_connection_fails_closed(self):
        save_routes({'verifier': 'gateway'})
        with self.assertRaises(ConnectionError):
            resolve_team()
        self.assertEqual(team_summary()['readiness'], 'pending')
        self.assertEqual(team_summary()['revision'], '')

    def test_broken_routing_and_unknown_roles_fail_closed(self):
        for raw in ('not json', '{"version":1,"roles":{"verifier":"missing"}}',
                    '{"version":1,"roles":{"hidden":"current"}}'):
            (self.path / 'team.json').write_text(raw, encoding='utf-8')
            with self.assertRaises(ConnectionError):
                resolve_team()
        with self.assertRaises(ConnectionError):
            save_routes({'proposer': '../escape'})

    def test_settings_routes_take_precedence_and_missing_roles_default_to_active(self):
        save_routes({'reviewer': 'gateway'})
        with self.settings(TEAM_ROLE_CONNECTIONS={'reviewer': 'kimi'}):
            connections = resolve_team()
        self.assertEqual(connections['reviewer'], self.kimi)
        self.assertEqual(connections['verifier'], self.deepseek)

    def test_revision_binds_all_roles_and_key_rotation(self):
        first = team_revision(resolve_team())
        save_routes({'reviewer': 'kimi'})
        second = team_revision(resolve_team())
        self.assertNotEqual(first, second)
        save_named_connection(ProviderConnection('kimi', self.kimi.model, 'rotated-test-key', 'test'))
        self.assertNotEqual(second, team_revision(resolve_team()))

    def test_all_connections_require_independent_evidence(self):
        save_routes({'reviewer': 'kimi'})
        self.approve(self.deepseek)
        self.assertTrue(chat_available(self.deepseek))
        self.assertFalse(chat_available(self.kimi))
        summary = team_summary()
        self.assertEqual(summary['readiness'], 'pending')
        self.assertFalse(next(item for item in summary['roles'] if item['role'] == 'reviewer')['ready'])
        self.approve(self.kimi)
        self.assertEqual(team_summary()['readiness'], 'available')
        CapabilityEvidence.objects.filter(connection_fingerprint=self.kimi.fingerprint).update(enabled=False)
        self.assertEqual(team_summary()['readiness'], 'pending')
        self.assertTrue(chat_available(self.deepseek))

    def test_summary_accepts_snapshot_and_never_exposes_secret_or_fingerprint(self):
        connections = resolve_team()
        with patch('delivery.team_config.resolve_team', side_effect=AssertionError('must use snapshot')):
            summary = team_summary(connections)
        serialized = json.dumps(summary)
        self.assertNotIn(self.deepseek.api_key, serialized)
        self.assertNotIn(self.deepseek.fingerprint, serialized)
        self.assertEqual(summary['max_calls'], 5)
        self.assertEqual(summary['max_output_tokens'], 4096)

    def test_command_persists_selected_roles_without_provider_call(self):
        output = StringIO()
        with patch('delivery.provider.generate_reply') as generate, patch('delivery.provider.list_models') as discover:
            call_command('configure_team', challenger='kimi', verifier='kimi', stdout=output)
        generate.assert_not_called()
        discover.assert_not_called()
        self.assertEqual(load_routes()['challenger'], 'kimi')
        self.assertEqual(load_routes()['verifier'], 'kimi')
        self.assertEqual(load_connection(), self.deepseek)
        self.assertNotIn(self.kimi.api_key, output.getvalue())

    def test_command_failure_preserves_previous_routes(self):
        save_routes({'reviewer': 'kimi'})
        before = (self.path / 'team.json').read_bytes()
        with self.assertRaises(CommandError):
            call_command('configure_team', reviewer='gateway', stdout=StringIO())
        self.assertEqual((self.path / 'team.json').read_bytes(), before)

    def test_command_cannot_silently_write_behind_settings_override(self):
        with self.settings(TEAM_ROLE_CONNECTIONS={'reviewer': 'kimi'}):
            with self.assertRaises(CommandError):
                call_command('configure_team', reviewer='current', stdout=StringIO())
        self.assertFalse((self.path / 'team.json').exists())

    def test_qwen_role_has_independent_acceptance_and_region_change_revises_consent(self):
        qwen = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing')
        save_named_connection(qwen)
        call_command('configure_team', reviewer='qwen', stdout=StringIO())
        self.assertEqual(resolve_team()['reviewer'], qwen)
        self.assertEqual(load_connection(), self.deepseek)
        self.approve(self.deepseek)
        self.assertEqual(team_summary()['readiness'], 'pending')
        self.approve(qwen)
        self.assertEqual(team_summary()['readiness'], 'available')
        original_revision = team_revision(resolve_team())
        save_named_connection(replace(qwen, service_region='ap-southeast-1'))
        self.assertNotEqual(team_revision(resolve_team()), original_revision)
        self.assertEqual(team_summary()['readiness'], 'pending')
        self.assertTrue(chat_available(self.deepseek))

    def test_qwen_platform_change_requires_fresh_team_consent_and_acceptance(self):
        qwen = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing')
        save_named_connection(qwen)
        call_command('configure_team', reviewer='qwen', stdout=StringIO())
        self.approve(self.deepseek)
        self.approve(qwen)
        before = team_revision(resolve_team())
        save_named_connection(replace(qwen, service_platform='bailian'))
        self.assertNotEqual(team_revision(resolve_team()), before)
        summary = team_summary()
        self.assertEqual(summary['readiness'], 'pending')
        reviewer = next(role for role in summary['roles'] if role['role'] == 'reviewer')
        self.assertIn('百炼', reviewer['provider_label'])
