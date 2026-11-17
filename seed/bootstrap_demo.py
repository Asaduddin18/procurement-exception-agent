"""Run schema migrations, seed the demo catalog and configure the MCP reader."""

import os

import psycopg
from psycopg import sql

from seed.seed import bootstrap


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    password = os.environ["MCP_DB_PASSWORD"]
    bootstrap(database_url)
    with psycopg.connect(database_url) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (767319028,))
        exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'procurement_mcp'"
        ).fetchone()
        if exists is None:
            connection.execute("CREATE ROLE procurement_mcp LOGIN")
        connection.execute(sql.SQL("ALTER ROLE procurement_mcp PASSWORD {}").format(sql.Literal(password)))
        connection.execute("GRANT procurement_mcp_reader TO procurement_mcp")
    print("Procurement schema, demo data and MCP reader are ready.")


if __name__ == "__main__":
    main()
