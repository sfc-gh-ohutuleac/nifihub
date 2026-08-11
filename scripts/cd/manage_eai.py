# Copyright 2026 Snowflake Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time


def _parse_account(account_url):
    host = account_url.replace("https://", "").replace("http://", "").rstrip("/")
    account = host.split(".snowflakecomputing.com")[0] if ".snowflakecomputing.com" in host else host
    return account, host


def snow_sql(sql, account_url, pat, user, role, database=None, schema=None, timeout=120):
    account, host = _parse_account(account_url)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".token", delete=False) as f:
        f.write(pat.strip())
        token_file = f.name
    try:
        cmd = [
            "snow", "sql", "-q", sql,
            "-x",
            "--account", account,
            "--host", host,
            "--user", user,
            "--authenticator", "PROGRAMMATIC_ACCESS_TOKEN",
            "--token-file-path", token_file,
            "--role", role,
            "--format", "json",
        ]
        if database:
            cmd.extend(["--database", database])
        if schema:
            cmd.extend(["--schema", schema])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    finally:
        os.unlink(token_file)
    if result.returncode != 0:
        raise RuntimeError(f"snow sql failed: {result.stderr}\nSQL: {sql}")
    if result.stdout.strip():
        return json.loads(result.stdout)
    return []


def fqn(database, schema, name):
    return f"{database}.{schema}.{name}"


REGISTRY_NR_BASE_NAME = "OPENFLOW_NIFIHUB_REGISTRY_NR"
REGISTRY_NR_VALUES = ["github.com:443", "api.github.com:443"]


def namespaced_nr_name(runtime_name, nr_name):
    return f"{runtime_name.upper()}_{nr_name.upper()}"


def eai_name_for_runtime(runtime_name):
    return f"OPENFLOW_{runtime_name}_EAI"


def create_network_rule(name, rule_type, mode, values, database, schema, **kwargs):
    print(f"[eai] Creating network rule '{name}'...", file=sys.stderr)
    value_list = ", ".join(f"'{v}'" for v in values)
    sql = (
        f"CREATE OR REPLACE NETWORK RULE {fqn(database, schema, name)} "
        f"TYPE = {rule_type} MODE = {mode} VALUE_LIST = ({value_list})"
    )
    print(f"[eai] Creating network rule {name}...")
    snow_sql(sql, database=database, schema=schema, **kwargs)
    print(f"[eai] Created network rule {name}")


def alter_network_rule(name, values, database, schema, **kwargs):
    print(f"[eai] Altering network rule '{name}'...", file=sys.stderr)
    value_list = ", ".join(f"'{v}'" for v in values)
    sql = (
        f"ALTER NETWORK RULE IF EXISTS {fqn(database, schema, name)} SET "
        f"VALUE_LIST = ({value_list})"
    )
    print(f"[eai] Altering network rule {name}...")
    snow_sql(sql, database=database, schema=schema, **kwargs)
    print(f"[eai] Altered network rule {name}")


def drop_network_rule(name, database, schema, **kwargs):
    print(f"[eai] Dropping network rule '{name}'...", file=sys.stderr)
    sql = f"DROP NETWORK RULE IF EXISTS {fqn(database, schema, name)}"
    print(f"[eai] Dropping network rule {name}...")
    snow_sql(sql, database=database, schema=schema, **kwargs)
    print(f"[eai] Dropped network rule {name}")


def eai_exists(name, **kwargs):
    try:
        rows = snow_sql(f"DESCRIBE EXTERNAL ACCESS INTEGRATION {name}", **kwargs)
        return bool(rows)
    except Exception:
        return False


def create_eai(name, network_rule_fqns, database, schema, grant_to_role=None, **kwargs):
    print(f"[eai] Creating external access integration '{name}'...", file=sys.stderr)
    rules = ", ".join(network_rule_fqns)
    if eai_exists(name, **kwargs):
        sql = (
            f"ALTER EXTERNAL ACCESS INTEGRATION IF EXISTS {name} SET "
            f"ALLOWED_NETWORK_RULES = ({rules})"
        )
        print(f"[eai] Altering EAI {name} with rules: {rules}...")
        snow_sql(sql, database=database, schema=schema, **kwargs)
        print(f"[eai] Altered EAI {name}")
    else:
        sql = (
            f"CREATE EXTERNAL ACCESS INTEGRATION {name} "
            f"ALLOWED_NETWORK_RULES = ({rules}) ENABLED = TRUE"
        )
        print(f"[eai] Creating EAI {name} with rules: {rules}...")
        snow_sql(sql, database=database, schema=schema, **kwargs)
        print(f"[eai] Created EAI {name}")
    if grant_to_role:
        grant_sql = f"GRANT USAGE ON INTEGRATION {name} TO ROLE {grant_to_role}"
        print(f"[eai] Granting USAGE on {name} to role {grant_to_role}...")
        snow_sql(grant_sql, **kwargs)
        print(f"[eai] Granted USAGE on {name} to role {grant_to_role}")


def drop_eai(name, **kwargs):
    print(f"[eai] Dropping external access integration '{name}'...", file=sys.stderr)
    sql = f"DROP EXTERNAL ACCESS INTEGRATION IF EXISTS {name}"
    print(f"[eai] Dropping EAI {name}...")
    snow_sql(sql, **kwargs)
    print(f"[eai] Dropped EAI {name}")


def create_runtime_eai(runtime_name, custom_network_rules, database, schema, execute_as_role=None, **kwargs):
    print(f"[eai] Creating EAI for runtime '{runtime_name}'...", file=sys.stderr)
    registry_nr = namespaced_nr_name(runtime_name, REGISTRY_NR_BASE_NAME)
    create_network_rule(
        registry_nr, "HOST_PORT", "EGRESS", REGISTRY_NR_VALUES,
        database, schema, **kwargs
    )
    for nr in (custom_network_rules or []):
        create_network_rule(
            namespaced_nr_name(runtime_name, nr["name"]), nr["type"], nr["mode"], nr["values"],
            database, schema, **kwargs
        )
    all_nr_fqns = [fqn(database, schema, registry_nr)]
    for nr in (custom_network_rules or []):
        all_nr_fqns.append(fqn(database, schema, namespaced_nr_name(runtime_name, nr["name"])))
    eai = eai_name_for_runtime(runtime_name)
    create_eai(eai, all_nr_fqns, database, schema, grant_to_role=execute_as_role, **kwargs)
    return eai


def delete_runtime_eai(runtime_name, custom_network_rules, database, schema, **kwargs):
    print(f"[eai] Deleting EAI for runtime '{runtime_name}'...", file=sys.stderr)
    eai = eai_name_for_runtime(runtime_name)
    drop_eai(eai, **kwargs)
    registry_nr = namespaced_nr_name(runtime_name, REGISTRY_NR_BASE_NAME)
    drop_network_rule(registry_nr, database, schema, **kwargs)
    for nr in (custom_network_rules or []):
        drop_network_rule(namespaced_nr_name(runtime_name, nr["name"]), database, schema, **kwargs)


def create_all(network_rules, eais, database, schema, **kwargs):
    for nr in (network_rules or []):
        create_network_rule(
            nr["name"], nr["type"], nr["mode"], nr["values"],
            database, schema, **kwargs
        )
    for eai in (eais or []):
        if isinstance(eai.get("network_rule"), list):
            create_eai(eai["name"], [fqn(database, schema, nr) for nr in eai["network_rule"]], database, schema, **kwargs)
        else:
            create_eai(eai["name"], [fqn(database, schema, eai["network_rule"])], database, schema, **kwargs)


def delete_all(eais, network_rules, **kwargs):
    for eai in (eais or []):
        drop_eai(eai["name"], **kwargs)
    for nr in (network_rules or []):
        drop_network_rule(nr["name"], **kwargs)


def main():
    parser = argparse.ArgumentParser(description="Manage EAIs and network rules")
    parser.add_argument("action", choices=["create", "delete"])
    parser.add_argument("--config", required=True, help="JSON config for network rules and EAIs")
    parser.add_argument("--database", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--account-url", required=True)
    parser.add_argument("--pat", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--role", required=True)
    args = parser.parse_args()

    config = json.loads(args.config)
    conn = {
        "account_url": args.account_url,
        "pat": args.pat,
        "user": args.user,
        "role": args.role,
    }

    if args.action == "create":
        create_all(
            config.get("network_rules", []),
            config.get("external_access_integrations", []),
            args.database, args.schema, **conn
        )
    elif args.action == "delete":
        delete_all(
            config.get("external_access_integrations", []),
            config.get("network_rules", []),
            database=args.database, schema=args.schema, **conn
        )


if __name__ == "__main__":
    main()