DO $$
BEGIN
    CREATE ROLE procurement_mcp_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
EXCEPTION
    WHEN duplicate_object OR unique_violation THEN NULL;
END
$$;

GRANT USAGE ON SCHEMA public TO procurement_mcp_reader;
GRANT SELECT ON public.vendors, public.price_catalog,
    public.sop_policy_versions, public.sop_policy_chunks TO procurement_mcp_reader;
