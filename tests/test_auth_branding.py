"""Managed login branding in infra/aws/auth.yaml matches the UMSAL brand (#57)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "aws" / "auth.yaml"
LOGO = ROOT / "infra" / "aws" / "branding" / "logo-symbol.png"
FAVICON = ROOT / "infra" / "aws" / "branding" / "favicon.ico"

FOREST_GREEN = "176b50ff"  # umsal-pwa docs/brand-identity.md
PRESSED_GREEN = "10553fff"
INK = "1b2e27ff"
# Cognito's default light-mode blues (primary, pressed, hover tint, active tint).
AWS_DEFAULT_BLUES = {"0972d3ff", "033160ff", "f2f8fdff", "d3e7f9ff"}


def _luminance(color: str) -> float:
    channels = [int(color[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


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
        path for path, value in _light_values(branding["Settings"]) if value in AWS_DEFAULT_BLUES
    ]
    assert leftovers == []


def test_light_mode_text_pairs_meet_wcag_aa(branding):
    settings = branding["Settings"]
    components = settings["components"]
    classes = settings["componentClasses"]
    page = components["pageBackground"]["lightMode"]["color"]
    form = components["form"]["lightMode"]["backgroundColor"]
    primary = components["primaryButton"]["lightMode"]
    match = classes["dropDown"]["lightMode"]["match"]
    pairs = {
        "primary button": (
            primary["defaults"]["textColor"],
            primary["defaults"]["backgroundColor"],
        ),
        "primary hover": (primary["hover"]["textColor"], primary["hover"]["backgroundColor"]),
        "heading on form": (components["pageText"]["lightMode"]["headingColor"], form),
        "body on form": (components["pageText"]["lightMode"]["bodyColor"], form),
        "description on page": (components["pageText"]["lightMode"]["descriptionColor"], page),
        "link on form": (classes["link"]["lightMode"]["defaults"]["textColor"], form),
        "dropdown match": (match["itemTextColor"], match["itemBackgroundColor"]),
    }
    low = {name: round(_contrast(fg, bg), 2) for name, (fg, bg) in pairs.items()}
    assert {name: ratio for name, ratio in low.items() if ratio < 4.5} == {}


def test_form_logo_is_the_repository_logo(branding):
    assert branding["Settings"]["components"]["form"]["logo"]["enabled"] is True
    (logo,) = [a for a in branding["Assets"] if a["Category"] == "FORM_LOGO"]
    assert (logo["ColorMode"], logo["Extension"]) == ("LIGHT", "PNG")
    assert base64.b64decode(logo["Bytes"]) == LOGO.read_bytes()


def test_favicon_is_the_repository_icon(branding):
    assert branding["Settings"]["components"]["favicon"]["enabledTypes"] == ["ICO"]
    (icon,) = [a for a in branding["Assets"] if a["Category"] == "FAVICON_ICO"]
    assert (icon["ColorMode"], icon["Extension"]) == ("LIGHT", "ICO")
    data = base64.b64decode(icon["Bytes"])
    assert data == FAVICON.read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00"  # ICO header: reserved 0, type 1 (icon)


def test_template_fits_inline_cloudformation_limit():
    # aws cloudformation deploy sends templates up to 51,200 bytes inline (no S3 upload).
    assert len(TEMPLATE.read_bytes()) < 51_200
