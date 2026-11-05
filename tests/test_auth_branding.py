"""Managed login branding in infra/aws/auth.yaml matches the UMSAL brand (#57)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "aws" / "auth.yaml"
LOGO = ROOT / "infra" / "aws" / "branding" / "logo-symbol.png"

FOREST_GREEN = "176b50ff"  # umsal-pwa docs/brand-identity.md
PRESSED_GREEN = "10553fff"
INK = "1b2e27ff"
AWS_DEFAULT_BLUE = "0972d3ff"


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation short-form tags (!Ref, !Sub, ...) as plain data."""


def _tag(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def branding() -> dict:
    template = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)  # noqa: S506
    return template["Resources"]["Branding"]["Properties"]


def _light_values(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            if key != "darkMode":
                yield from _light_values(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _light_values(value, f"{path}[{index}]")
    else:
        yield path, node


def test_uses_custom_settings_not_cognito_defaults(branding):
    assert branding["UseCognitoProvidedValues"] is False
    assert branding["Settings"]["categories"]["global"]["colorSchemeMode"] == "LIGHT"


def test_primary_actions_use_forest_green(branding):
    components = branding["Settings"]["components"]
    primary = components["primaryButton"]["lightMode"]
    assert primary["defaults"]["backgroundColor"] == FOREST_GREEN
    assert primary["hover"]["backgroundColor"] == PRESSED_GREEN
    assert (
        branding["Settings"]["componentClasses"]["link"]["lightMode"]["defaults"]["textColor"]
        == FOREST_GREEN
    )
    assert components["pageText"]["lightMode"]["headingColor"] == INK


def test_no_aws_default_blue_left_in_light_mode(branding):
    leftovers = [
        path for path, value in _light_values(branding["Settings"]) if value == AWS_DEFAULT_BLUE
    ]
    assert leftovers == []


def test_form_logo_is_the_repository_logo(branding):
    assert branding["Settings"]["components"]["form"]["logo"]["enabled"] is True
    (logo,) = [a for a in branding["Assets"] if a["Category"] == "FORM_LOGO"]
    assert (logo["ColorMode"], logo["Extension"]) == ("LIGHT", "PNG")
    assert base64.b64decode(logo["Bytes"]) == LOGO.read_bytes()


def test_template_fits_inline_cloudformation_limit():
    # aws cloudformation deploy sends templates up to 51,200 bytes inline (no S3 upload).
    assert len(TEMPLATE.read_bytes()) < 51_200
