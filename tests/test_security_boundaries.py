"""Local path, authority and network boundaries without live connections."""
import os
import socket

import pytest

from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import (
    ClassifiedText, DataClass, authorize, validate_public_url, verify_policy,
)


@pytest.mark.parametrize('path', ['../escape', '/tmp/escape', 'x/../../escape', 'x//a', './x', 'x\\y', ''])
def test_workspace_path_escape_denied(workspace, path):
    with pytest.raises(BoundaryError):
        workspace.write(path, b'fixture')


def test_symlink_and_hardlink_denied(workspace):
    workspace.write('safe', b'original')
    (workspace.root / 'linked').symlink_to(workspace.root / 'safe')
    with pytest.raises(BoundaryError):
        workspace.write('linked', b'changed', replace=True)
    os.link(workspace.root / 'safe', workspace.root / 'hardlinked')
    with pytest.raises(BoundaryError):
        workspace.write('hardlinked', b'changed', replace=True)
    assert (workspace.root / 'safe').read_bytes() == b'original'


@pytest.mark.parametrize('action', ['subscribe', 'unsubscribe', 'follow', 'cloud', 'desktop',
                                     'canary', 'write_project', 'install_external_code'])
def test_no_implicit_authority(action):
    with pytest.raises(BoundaryError):
        authorize(action, payload=ClassifiedText(text='fixture', classification=DataClass.PUBLIC))


def test_private_input_cannot_be_public_query():
    private = ClassifiedText(text='local context', classification=DataClass.PRIVATE)
    public = ClassifiedText(text='public source', classification=DataClass.PUBLIC)
    with pytest.raises(BoundaryError): authorize('public_read', payload=private)
    with pytest.raises(BoundaryError):
        authorize('public_read', payload=ClassifiedText.derive('summary', [public, private]))
    authorize('public_read', payload=public)


def test_policy_drift_fails_closed(workspace):
    workspace.write('config/policies/safety.json', b'{}', replace=True)
    with pytest.raises(BoundaryError): verify_policy(workspace)


def resolver(address):
    return lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]


@pytest.mark.parametrize('address', ['127.0.0.1', '10.1.2.3', '192.168.1.4',
                                     '169.254.169.254', '::1', '::ffff:127.0.0.1'])
def test_private_dns_rejected(address):
    with pytest.raises(BoundaryError):
        validate_public_url('https://example.org', resolver=resolver(address))


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://example.org',
                                 'https://localhost', 'https://user:pass@example.org',
                                 'https://example.org:8080'])
def test_unsafe_url_rejected(url):
    with pytest.raises(BoundaryError):
        validate_public_url(url, resolver=resolver('93.184.216.34'))
