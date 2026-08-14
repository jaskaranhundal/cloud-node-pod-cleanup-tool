"""Unit tests for the pure pod-classification logic (no cluster needed)."""
import importlib
import os
import types
from unittest import mock

import pytest

import control_and_cleanup as c


def _pod(name, phase="Running", ready=True, deletion=None, owners=None, annotations=None):
    conds = [types.SimpleNamespace(type="Ready", status="True" if ready else "False")]
    meta = types.SimpleNamespace(name=name, namespace="ns", deletion_timestamp=deletion,
                                 owner_references=owners, annotations=annotations)
    status = types.SimpleNamespace(phase=phase, conditions=conds)
    return types.SimpleNamespace(metadata=meta, status=status)


def test_get_base_name():
    assert c.get_base_name("web-5f9c8d-x2") == "web"
    assert c.get_base_name("my-app-x") == "my-app-x"


def test_is_healthy():
    assert c._is_healthy(_pod("p")) is True
    assert c._is_healthy(_pod("p", ready=False)) is False
    assert c._is_healthy(_pod("p", phase="Pending")) is False
    assert c._is_healthy(_pod("p", deletion="2026-07-07")) is False


def test_is_evictable():
    assert c._is_evictable(_pod("p")) is True
    assert c._is_evictable(_pod("p", owners=[types.SimpleNamespace(kind="DaemonSet")])) is False
    assert c._is_evictable(_pod("p", annotations={"kubernetes.io/config.mirror": "x"})) is False
    assert c._is_evictable(_pod("p", deletion="2026-07-07")) is False


def _reload_with(env):
    with mock.patch.dict(os.environ, env, clear=False):
        return importlib.reload(c)


def test_namespaces_has_no_default():
    """A pod-deleting tool must not fall back to some other cluster's namespaces."""
    mod = _reload_with({"NAMESPACES": ""})
    assert mod.NAMESPACES == []


def test_namespaces_parsed_and_trimmed():
    mod = _reload_with({"NAMESPACES": " team-production , team-staging "})
    assert mod.NAMESPACES == ["team-production", "team-staging"]


def test_main_exits_when_namespaces_unset(monkeypatch):
    mod = _reload_with({"NAMESPACES": ""})
    monkeypatch.setattr(mod.sys, "argv", ["control_and_cleanup.py", "stop"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
