"""Resolve the auth template's two deployment modes without contacting AWS (#77)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NATIVE = "kr.ac.smu.umsal.inputprobe://oauth/callback"


class CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {suffix: value}


CfnLoader.add_multi_constructor("!", _tag)


def _resolve(node, parameters, conditions):
    if isinstance(node, list):
        return [_resolve(item, parameters, conditions) for item in node]
    if not isinstance(node, dict):
        return node
    if "Ref" in node:
        return parameters[node["Ref"]]
    if "Equals" in node:
        left, right = _resolve(node["Equals"], parameters, conditions)
        return left == right
    if "Not" in node:
        return not _resolve(node["Not"][0], parameters, conditions)
    if "If" in node:
        condition, yes, no = node["If"]
        branch = yes if _resolve(conditions[condition], parameters, conditions) else no
        return _resolve(branch, parameters, conditions)
    raise AssertionError(f"Unsupported intrinsic in callback configuration: {list(node)}")


@pytest.mark.parametrize("native", ["", NATIVE])
def test_callback_modes_preserve_web_login_and_public_pkce_client(native):
    template = yaml.load((ROOT / "infra/aws/auth.yaml").read_text(), Loader=CfnLoader)
    parameters = {key: value["Default"] for key, value in template["Parameters"].items()}
    parameters["NativeCallbackUrl"] = native
    client = template["Resources"]["WebClient"]["Properties"]
    callbacks = _resolve(client["CallbackURLs"], parameters, template["Conditions"])
    assert callbacks == [parameters["CallbackUrl"]] + ([NATIVE] if native else [])
    assert _resolve(client["LogoutURLs"], parameters, {}) == [parameters["LogoutUrl"]]
    assert client["GenerateSecret"] is False
    assert client["AllowedOAuthFlows"] == ["code"]
    assert client["AllowedOAuthScopes"] == ["openid", "email"]
    assert client["EnableTokenRevocation"] is True
    assert template["Parameters"]["NativeCallbackUrl"]["AllowedValues"] == ["", NATIVE]


def test_demo_parameters_reproduce_the_observed_web_and_android_callbacks():
    values = json.loads((ROOT / "infra/aws/auth-demo.parameters.json").read_text())
    parameters = {entry["ParameterKey"]: entry["ParameterValue"] for entry in values}
    assert len(parameters) == len(values) == 4
    assert parameters["NativeCallbackUrl"] == NATIVE
    assert parameters["CallbackUrl"] == "https://d86lxzcp0rvt4.cloudfront.net/auth/callback"
    assert parameters["LogoutUrl"] == "https://d86lxzcp0rvt4.cloudfront.net/"
