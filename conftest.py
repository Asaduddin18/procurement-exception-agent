"""Disposable PostgreSQL databases shared by integration test suites."""

import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest


@pytest.fixture
def database_url():
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests.")
    name = f"procurement_test_{uuid4().hex}"
    database = sql.Identifier(name)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(database))
        try:
            yield make_conninfo(admin_url, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(database))
