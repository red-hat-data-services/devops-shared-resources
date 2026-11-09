"""Tests for package responsibilities."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path
import subprocess
import sys
from lib.gap_notifications import notify_tools
from gap_notification_fakes import observation


# Package

# Packaged templates must render outside the checkout without constructing service clients.
def test_packaged_template_and_real_formatting_work_outside_checkout(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('pure formatting must not construct clients')
    monkeypatch.setattr(notify_tools, 'JiraClient', forbidden)
    monkeypatch.setattr(notify_tools, 'SlackClient', forbidden)
    monkeypatch.chdir(tmp_path)
    template = files('lib.gap_notifications').joinpath('templates/issue.md.j2')
    assert template.is_file() and template.read_text().startswith('# {{ notification_heading }}')
    markdown = notify_tools.render_markdown(observation())
    assert '# GAP STAGING TEST' in markdown
    assert 'Repository: ' + notify_tools.literal('acme/repo') in markdown
    assert notify_tools.description(observation())['type'] == 'doc'
    slack_template = files('lib.gap_notifications').joinpath('templates/slack.md.j2')
    assert slack_template.is_file()
    slack = notify_tools.slack_message(observation(), jira_url='https://stage-redhat.atlassian.net/browse/RHAI-1')
    assert '*GAP · Build failed — staging test*' in slack
    assert '*Repository:* `acme/repo`' in slack


# Importing the package initializer must not eagerly initialize notification modules or clients.
def test_minimal_package_import_has_no_notification_initialization():
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(('lib.gap_notifications.', 'lib.jira_client', 'lib.slack_client')):
        raise AssertionError('eager package import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import lib.gap_notifications
assert not hasattr(lib.gap_notifications, 'Dispatcher')
assert not hasattr(lib.gap_notifications, 'JiraNotifier')
"""
    subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[2], check=True)
