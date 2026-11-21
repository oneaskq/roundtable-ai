from django.test import SimpleTestCase

from manage import prepare_settings


class ManagementSettingsTests(SimpleTestCase):
    def test_default_test_command_is_isolated_even_with_inherited_live_settings(self):
        for environment in ({}, {'DJANGO_SETTINGS_MODULE': 'config.settings'}):
            prepare_settings(['manage.py', 'test'], environment)
            self.assertEqual(environment['DJANGO_SETTINGS_MODULE'], 'config.test_settings')

    def test_explicit_test_settings_and_other_commands_remain_operator_controlled(self):
        environment = {'DJANGO_SETTINGS_MODULE': 'custom.settings'}
        prepare_settings(['manage.py', 'test', '--settings=custom.tests'], environment)
        self.assertEqual(environment['DJANGO_SETTINGS_MODULE'], 'custom.settings')
        prepare_settings(['manage.py', 'test', '--settings', 'custom.tests'], environment)
        self.assertEqual(environment['DJANGO_SETTINGS_MODULE'], 'custom.settings')
        prepare_settings(['manage.py', 'runserver'], environment)
        self.assertEqual(environment['DJANGO_SETTINGS_MODULE'], 'custom.settings')
        environment = {}
        prepare_settings(['manage.py', 'runserver'], environment)
        self.assertEqual(environment['DJANGO_SETTINGS_MODULE'], 'config.settings')
