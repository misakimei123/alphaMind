-- P3-04 PostgreSQL 17 权限模板。角色必须由 secret manager/DBA 预先创建，本文件不保存口令。
-- psql 调用前设置：runtime_database、runtime_schema、owner_role、watchdog_role。

REVOKE CONNECT ON DATABASE :"runtime_database" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"runtime_database" TO :"owner_role", :"watchdog_role";

CREATE SCHEMA IF NOT EXISTS :"runtime_schema" AUTHORIZATION :"owner_role";
REVOKE ALL ON SCHEMA :"runtime_schema" FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA :"runtime_schema" TO :"owner_role";
GRANT USAGE ON SCHEMA :"runtime_schema" TO :"watchdog_role";

-- 只有 Freqtrade owner 能创建/迁移 Runtime schema；watchdog 强制只读。
ALTER ROLE :"owner_role" SET search_path TO :"runtime_schema";
ALTER ROLE :"watchdog_role" SET search_path TO :"runtime_schema";
ALTER ROLE :"watchdog_role" SET default_transaction_read_only = on;
REVOKE CREATE ON SCHEMA :"runtime_schema" FROM :"watchdog_role";

GRANT SELECT ON ALL TABLES IN SCHEMA :"runtime_schema" TO :"watchdog_role";
GRANT SELECT ON ALL SEQUENCES IN SCHEMA :"runtime_schema" TO :"watchdog_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA :"runtime_schema"
    GRANT SELECT ON TABLES TO :"watchdog_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA :"runtime_schema"
    GRANT SELECT ON SEQUENCES TO :"watchdog_role";
