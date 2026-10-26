"""Behaviour tests for the rules of the Project entity: both sides of every rule and the action."""

import pytest


def create(client, headers, name="p", archived=False):
    return client.post("/projects", json={"name": name, "archived": archived}, headers=headers)


@pytest.mark.rule("project-limit")
def test_the_21st_active_project_is_rejected(client, user_a):
    for i in range(20):
        assert create(client, user_a, f"p{i}").status_code == 201
    response = create(client, user_a, "one too many")
    assert response.status_code == 409
    assert response.json()["rule"] == "project-limit"


@pytest.mark.rule("project-limit")
def test_archived_projects_and_other_users_do_not_count(client, user_a, user_b):
    for i in range(20):
        assert create(client, user_a, f"a{i}", archived=True).status_code == 201
    assert create(client, user_a, "active").status_code == 201
    assert create(client, user_b, "b1").status_code == 201


@pytest.mark.rule("already-archived")
def test_archiving_twice_is_a_conflict(client, user_a):
    pid = create(client, user_a).json()["id"]
    first = client.post(f"/projects/{pid}/archive", headers=user_a)
    assert first.status_code == 200 and first.json()["archived"] is True
    second = client.post(f"/projects/{pid}/archive", headers=user_a)
    assert second.status_code == 409 and second.json()["rule"] == "already-archived"
