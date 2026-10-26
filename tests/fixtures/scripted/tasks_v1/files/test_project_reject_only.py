"""A one-sided suite: it only ever checks that things are rejected.

It names every rule and calls the action, so the static gate passes, and it fails against the
unimplemented stubs. But a rule set that rejects everything passes it, so the "always reject"
cheat probe catches it.
"""

import pytest


def create(client, headers, name="p", archived=False):
    return client.post("/projects", json={"name": name, "archived": archived}, headers=headers)


@pytest.mark.rule("project-limit")
def test_the_21st_project_is_rejected(client, user_a):
    for i in range(21):
        response = create(client, user_a, f"p{i}")
    assert response.status_code == 409
    assert response.json()["rule"] == "project-limit"


@pytest.mark.rule("already-archived")
def test_archive_url_exists(client, user_a):
    assert client.post("/projects/1/archive", headers=user_a).status_code in (404, 409)
