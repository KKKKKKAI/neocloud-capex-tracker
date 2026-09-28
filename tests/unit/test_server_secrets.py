"""SSM -> env-file loader. Uses botocore's Stubber: no AWS calls."""
from __future__ import annotations

import stat

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.stub import Stubber  # noqa: E402

from capex.server import secrets  # noqa: E402

TOKEN = "sk-test-not-a-real-token"


def _client():
    return boto3.client(
        "ssm", region_name="eu-north-1",
        aws_access_key_id="testing", aws_secret_access_key="testing",
    )


def _param(name, value, type_="SecureString"):
    return {"Name": f"/capex/{name}", "Type": type_, "Value": value}


def _stub_get(client, params):
    stubber = Stubber(client)
    stubber.add_response(
        "get_parameters_by_path",
        {"Parameters": params},
        {"Path": "/capex/", "Recursive": False, "WithDecryption": True},
    )
    stubber.activate()
    return stubber


def test_fetch_writes_group_readable_env_file(tmp_path, capsys):
    client = _client()
    _stub_get(client, [
        _param("CLAUDE_CODE_OAUTH_TOKEN", TOKEN),
        _param("ALPHA_VANTAGE_API_KEY", "AVKEY123"),
        _param("GMAIL_USERNAME", "ops@example.com", "String"),
        _param("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop"),
    ])
    out = tmp_path / "run" / "capex.env"

    assert secrets.main(["fetch", "--out", str(out)], client=client) == 0

    assert out.read_text().splitlines() == [
        "ALPHA_VANTAGE_API_KEY='AVKEY123'",
        f"CLAUDE_CODE_OAUTH_TOKEN='{TOKEN}'",
        "GMAIL_APP_PASSWORD='abcd efgh ijkl mnop'",
        "GMAIL_USERNAME='ops@example.com'",
    ]
    assert stat.S_IMODE(out.stat().st_mode) == 0o640
    printed = capsys.readouterr()
    assert TOKEN not in printed.out + printed.err
    assert "AVKEY123" not in printed.out + printed.err


def test_fetch_refuses_when_a_required_secret_is_missing(tmp_path, capsys):
    client = _client()
    _stub_get(client, [_param("ALPHA_VANTAGE_API_KEY", "AVKEY123")])
    out = tmp_path / "capex.env"

    assert secrets.main(["fetch", "--out", str(out)], client=client) == 1
    assert not out.exists()
    assert "CLAUDE_CODE_OAUTH_TOKEN" in capsys.readouterr().err


def test_fetch_rejects_values_it_cannot_quote_safely(tmp_path, capsys):
    client = _client()
    _stub_get(client, [
        _param("CLAUDE_CODE_OAUTH_TOKEN", "it's-broken"),
        _param("ALPHA_VANTAGE_API_KEY", "AVKEY123"),
    ])
    out = tmp_path / "capex.env"

    assert secrets.main(["fetch", "--out", str(out)], client=client) == 1
    assert not out.exists()
    err = capsys.readouterr().err
    assert "CLAUDE_CODE_OAUTH_TOKEN" in err
    assert "it's-broken" not in err


def test_fetch_skips_names_that_are_not_env_vars(tmp_path):
    client = _client()
    _stub_get(client, [
        _param("CLAUDE_CODE_OAUTH_TOKEN", TOKEN),
        _param("ALPHA_VANTAGE_API_KEY", "AVKEY123"),
        _param("not-an-env-name", "x"),
    ])
    out = tmp_path / "capex.env"
    assert secrets.main(["fetch", "--out", str(out)], client=client) == 0
    assert "not-an-env-name" not in out.read_text()


def test_render_env_single_quotes_values():
    assert secrets.render_env({"B": "has $dollar and spaces", "A": "x"}) == (
        "A='x'\nB='has $dollar and spaces'\n"
    )


def _stub_describe(client, params):
    stubber = Stubber(client)
    stubber.add_response(
        "describe_parameters",
        {"Parameters": [{"Name": f"/capex/{n}", "Type": t} for n, t in params]},
        {"ParameterFilters": [{"Key": "Name", "Option": "BeginsWith", "Values": ["/capex/"]}]},
    )
    stubber.activate()


def test_check_flags_plain_string_secrets_and_missing_ones(capsys):
    client = _client()
    _stub_describe(client, [
        ("CLAUDE_CODE_OAUTH_TOKEN", "String"),
        ("ALPHA_VANTAGE_API_KEY", "SecureString"),
        ("GMAIL_USERNAME", "String"),
    ])
    assert secrets.main(["check"], client=client) == 0  # required ones exist
    out = capsys.readouterr().out
    assert "CLAUDE_CODE_OAUTH_TOKEN is a String; store it as SecureString" in out
    assert "GMAIL_USERNAME is a String" not in out  # not a secret
    assert "optional parameter GMAIL_APP_PASSWORD is not set" in out


def test_check_fails_when_required_parameter_missing(capsys):
    client = _client()
    _stub_describe(client, [("ALPHA_VANTAGE_API_KEY", "SecureString")])
    assert secrets.main(["check"], client=client) == 1
    assert "missing required parameter CLAUDE_CODE_OAUTH_TOKEN" in capsys.readouterr().out
