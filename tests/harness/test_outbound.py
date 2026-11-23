"""What reference reads may send off the machine, and where they may land.

A fake site stands in for the network: it serves one page, redirects one path
to another page on the site and one to a host off the allowlist.
"""

from __future__ import annotations

import httpx
import pytest
from meddataos.harness.outbound import OutboundScreen
from meddataos.harness.references import References, Site, without_query
from meddataos.harness.tools import HarnessTools

PAGE = "<html><head><title>Staging</title></head><body><main><p>{}</p></main></body></html>"
SITE = Site(name="ra", base_url="https://ra.example/", query_params=("page",))


def screen(*identifiers: str) -> OutboundScreen:
    return OutboundScreen(lambda: identifiers)


# -- the screen -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://ra.example/search/RADCURE-0006",
        "https://ra.example/search/radcure_0006",
        "https://ra.example/search/radcure%200006",
        "https://ra.example/search/RADCURE0006",
        "https://ra.example/q/Jane+Doe/history",
        "https://ra.example/q/1958-03-14",
        "https://ra.example/q/14/03/1958",
    ],
)
def test_a_url_naming_the_patient_or_a_date_is_refused(url):
    assert screen("RADCURE-0006", "Jane Doe").problem(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://ra.example/head-neck/oropharynx-staging",
        # A surname alone, or inside another word, is not the name.
        "https://ra.example/doe-classification",
        "https://ra.example/janedoeville",
        "https://ra.example/articles/2019",
    ],
)
def test_an_ordinary_reference_url_passes(url):
    assert screen("RADCURE-0006", "Jane Doe").problem(url) is None


def test_the_reason_never_repeats_the_identifier():
    problem = screen("RADCURE-0006").problem("https://ra.example/RADCURE-0006")

    assert "RADCURE" not in problem and "0006" not in problem


def test_identifiers_are_asked_each_time_since_the_card_fills_in_later():
    known: list[str] = []
    later = OutboundScreen(lambda: known)

    assert later.problem("https://ra.example/q/jane-doe") is None
    known.append("Jane Doe")
    assert later.problem("https://ra.example/q/jane-doe")


# -- redirects and query strings --------------------------------------------


def fake_site(request: httpx.Request) -> httpx.Response:
    if request.url.host != "ra.example":
        return httpx.Response(200, html=PAGE.format("evil"))

    if request.url.path == "/moved":
        return httpx.Response(301, headers={"location": "/staging"})

    if request.url.path == "/out":
        return httpx.Response(302, headers={"location": "https://evil.example/collect"})

    if request.url.path == "/loop":
        return httpx.Response(302, headers={"location": "/loop"})

    return httpx.Response(
        200,
        html=PAGE.format(f"path {request.url.path} query {request.url.query.decode()}"),
    )


@pytest.fixture
def references(tmp_path):
    client = httpx.Client(
        transport=httpx.MockTransport(fake_site), follow_redirects=False
    )
    return References(sites=[SITE], cache=tmp_path, client=client)


def test_a_redirect_on_the_site_is_followed(references):
    assert "path /staging" in references.read("https://ra.example/moved")["text"]


def test_a_redirect_off_the_allowlist_is_not_followed(references):
    assert references.read("https://ra.example/out") == {
        "error": "Could not fetch https://ra.example/out"
    }


def test_a_redirect_loop_stops(references):
    assert "error" in references.read("https://ra.example/loop")


def test_query_strings_are_dropped_unless_the_site_lists_them(references):
    read = references.read("https://ra.example/staging?q=jane+doe&page=2")

    assert read["url"] == "https://ra.example/staging?page=2"
    assert "query page=2" in read["text"]
    assert "dropped" in read["note"]


def test_a_url_without_a_query_is_untouched():
    assert without_query("https://ra.example/a/b") == "https://ra.example/a/b"


def test_reference_read_screens_before_it_fetches(tmp_path):
    fetched: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, html=PAGE.format("x"))

    client = httpx.Client(transport=httpx.MockTransport(record))
    tools = HarnessTools(
        skills_dir=tmp_path / "none",
        references=References(sites=[SITE], cache=tmp_path, client=client),
        outbound=screen("RADCURE-0006"),
    )

    refused = tools.run("reference_read", {"url": "https://ra.example/RADCURE-0006"})

    assert refused["status"] == "error" and "never the patient" in refused["message"]
    assert fetched == []
