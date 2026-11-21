#!/usr/bin/env python
import os
import sys


def prepare_settings(argv, environ):
    """Run the default test command with isolated keys, connections and locks."""
    args = argv[2:]
    explicit_settings = any(arg == '--settings' or arg.startswith('--settings=') for arg in args)
    if len(argv) > 1 and argv[1] == 'test' and not explicit_settings:
        # An inherited live-service environment must not make a routine test
        # read real provider credentials or contend with the live worker lock.
        environ['DJANGO_SETTINGS_MODULE'] = 'config.test_settings'
    else:
        environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')


if __name__ == '__main__':
    prepare_settings(sys.argv, os.environ)
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
