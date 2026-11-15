"""Provision a disposable login with the same grants as the MCP service."""

from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from seed.seed import bootstrap


@pytest.fixture
def reader_url(database_url):
    bootstrap(database_url)
    username = f"mcp_test_{uuid4().hex}"
    password = uuid4().hex
    identifier = sql.Identifier(username)
    with psycopg.connect(database_url, autocommit=True) as owner:
        owner.execute(sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} IN ROLE procurement_mcp_reader"
        ).format(identifier, sql.Literal(password)))
        try:
            yield make_conninfo(database_url, user=username, password=password)
        finally:
            owner.execute(sql.SQL("DROP ROLE {}").format(identifier))
