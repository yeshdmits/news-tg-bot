"""The spec acquisition chain: SPEC_JSON > SPEC_URL > SPEC_PATH, strict
no-fall-through on failure, and the no-source error message."""

import hashlib
from pathlib import Path

import httpx
import pytest

from cli import Settings
from feedspec.loader import SpecError
from specsource import (
    SpecSourceError,
    acquire_spec,
    choose_source,
    load_spec_for,
    spec_base_dir,
)
from tests.conftest import SPEC_FIXTURE

SPEC_BYTES = Path(SPEC_FIXTURE).read_bytes()


def transport_serving(content: bytes, status_code: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(status_code, content=content)
    )


def failing_transport() -> httpx.MockTransport:
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    return httpx.MockTransport(handler)


class RecordingTransport(httpx.MockTransport):
    """MockTransport that records whether it was ever consulted."""

    def __init__(self):
        self.calls = 0

        def handler(request):
            self.calls += 1
            return httpx.Response(200, content=SPEC_BYTES)

        super().__init__(handler)


# --- each source alone --------------------------------------------------------


def test_path_source_alone():
    settings = Settings(spec_path=SPEC_FIXTURE)
    source, raw = acquire_spec(settings)
    assert source.kind == "path"
    assert raw == SPEC_BYTES
    assert load_spec_for(settings).spec.version == 2


def test_inline_source_alone():
    text = SPEC_BYTES.decode()
    settings = Settings(spec_json=text)
    source, raw = acquire_spec(settings)
    assert source.kind == "inline"
    # The hash is over the exact env-string bytes — no re-serialisation.
    assert raw == text.encode("utf-8")
    loaded = load_spec_for(settings)
    assert loaded.spec_hash == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_inline_location_never_contains_content():
    settings = Settings(spec_json=SPEC_BYTES.decode())
    source = choose_source(settings)
    assert "sources" not in source.location
    assert source.location == "<env:SPEC_JSON>"


def test_url_source_alone():
    settings = Settings(spec_url="https://config.example.org/spec.json")
    loaded = load_spec_for(settings, transport=transport_serving(SPEC_BYTES))
    assert loaded.spec.version == 2
    assert loaded.spec_hash == hashlib.sha256(SPEC_BYTES).hexdigest()


# --- precedence ----------------------------------------------------------------


def test_inline_beats_url_and_path(tmp_path):
    transport = RecordingTransport()
    settings = Settings(
        spec_json=SPEC_BYTES.decode(),
        spec_url="https://config.example.org/spec.json",
        spec_path=SPEC_FIXTURE,
    )
    source, _ = acquire_spec(settings, transport=transport)
    assert source.kind == "inline"
    assert transport.calls == 0


def test_url_beats_path():
    settings = Settings(
        spec_url="https://config.example.org/spec.json",
        spec_path="does/not/exist.json",
    )
    source, raw = acquire_spec(settings, transport=transport_serving(SPEC_BYTES))
    assert source.kind == "url"
    assert raw == SPEC_BYTES


def test_explicit_override_path_beats_everything():
    settings = Settings(spec_json='{"not": "used"}')
    source, raw = acquire_spec(settings, override_path=SPEC_FIXTURE)
    assert source.kind == "path"
    assert raw == SPEC_BYTES


# --- failures never fall through ------------------------------------------------


def test_malformed_inline_does_not_fall_through_to_valid_path():
    settings = Settings(spec_json="{ not json", spec_path=SPEC_FIXTURE)
    with pytest.raises(SpecError, match="not valid JSON"):
        load_spec_for(settings)


def test_url_non_200_fails_without_fallback():
    settings = Settings(
        spec_url="https://config.example.org/spec.json",
        spec_path=SPEC_FIXTURE,
    )
    with pytest.raises(SpecSourceError, match="HTTP 404"):
        acquire_spec(settings, transport=transport_serving(b"gone", status_code=404))


def test_url_transport_error_fails_without_fallback():
    settings = Settings(
        spec_url="https://config.example.org/spec.json",
        spec_path=SPEC_FIXTURE,
    )
    with pytest.raises(SpecSourceError, match="fetch failed"):
        acquire_spec(settings, transport=failing_transport())


def test_url_body_that_does_not_parse_is_fatal():
    settings = Settings(
        spec_url="https://config.example.org/spec.json",
        spec_path=SPEC_FIXTURE,
    )
    with pytest.raises(SpecError, match="not valid JSON"):
        load_spec_for(settings, transport=transport_serving(b"<html>maintenance</html>"))


def test_non_https_url_rejected():
    settings = Settings(spec_url="http://config.example.org/spec.json")
    with pytest.raises(SpecSourceError, match="https"):
        acquire_spec(settings)


def test_unreadable_path_is_a_source_error():
    settings = Settings(spec_path="does/not/exist.json")
    with pytest.raises(SpecSourceError, match="cannot read spec file"):
        acquire_spec(settings)


# --- no source configured -------------------------------------------------------


def test_no_source_error_names_all_three_options():
    with pytest.raises(SpecSourceError) as exc:
        choose_source(Settings())
    message = str(exc.value)
    for name in ("SPEC_JSON", "SPEC_URL", "SPEC_PATH", "spec.example.json"):
        assert name in message


# --- base dir for XSD resolution -------------------------------------------------


def test_base_dir_is_spec_parent_for_path_sources():
    settings = Settings(spec_path=SPEC_FIXTURE)
    source = choose_source(settings)
    assert spec_base_dir(settings, source) == str(Path(SPEC_FIXTURE).parent)


def test_base_dir_defaults_to_cwd_for_inline_sources():
    settings = Settings(spec_json="{}")
    source = choose_source(settings)
    assert spec_base_dir(settings, source) == "."


def test_base_dir_env_override_wins():
    settings = Settings(spec_json="{}", spec_base_dir="/app")
    source = choose_source(settings)
    assert spec_base_dir(settings, source) == "/app"
