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


def snow_sql(sql, account_url, pat, user, role, timeout=300):
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


def create_runtime(name, deployment, database, schema, node_type, min_nodes,
                   max_nodes, execute_as_role, eai_names=None,
                   display_name=None, comment=None, **kwargs):
    print(f"[runtime] Creating runtime '{name}' in deployment '{deployment}'...", file=sys.stderr)
    runtime_fqn = fqn(database, schema, name)
    sql = (
        f"CREATE OPENFLOW RUNTIME {runtime_fqn} "
        f"IN DEPLOYMENT {deployment} "
        f"NODE_TYPE = {node_type} "
        f"MIN_NODES = {min_nodes} "
        f"MAX_NODES = {max_nodes} "
        f"EXECUTE_AS_ROLE = {execute_as_role}"
    )
    if eai_names:
        eai_list = ", ".join(eai_names)
        sql += f" EXTERNAL_ACCESS_INTEGRATIONS = ({eai_list})"
    if display_name:
        sql += f" DISPLAY_NAME = '{display_name}'"
    if comment:
        sql += f" COMMENT = '{comment}'"
    print(f"[runtime] Creating {runtime_fqn}...")
    try:
        snow_sql(sql, timeout=900, **kwargs)
    except (subprocess.TimeoutExpired, TimeoutError):
        print(f"[runtime] CREATE command timed out, polling for status...")
    except RuntimeError as e:
        err = str(e).lower()
        if "already exists" in err:
            print(f"[runtime] {runtime_fqn} already exists — ensuring it is ACTIVE")
            resume_runtime(name, database, schema, **kwargs)
            return
        if "timed out" in err:
            print(f"[runtime] CREATE command timed out, polling for status...")
        else:
            raise
    print(f"[runtime] Waiting for {runtime_fqn} to become ACTIVE...")
    wait_runtime_status(name, database, schema, ["ACTIVE", "CREATE_FAILED", "ACTIVATE_FAILED"], timeout=1200, **kwargs)


def describe_runtime(name, database, schema, **kwargs):
    runtime_fqn = fqn(database, schema, name)
    rows = snow_sql(f"DESCRIBE OPENFLOW RUNTIME {runtime_fqn}", **kwargs)
    if rows:
        return rows[0] if isinstance(rows, list) else rows
    return None


def wait_runtime_status(name, database, schema, target_statuses, timeout=600, **kwargs):
    runtime_fqn = fqn(database, schema, name)
    start = time.time()
    while time.time() - start < timeout:
        try:
            desc = describe_runtime(name, database, schema, **kwargs)
        except Exception as e:
            print(f"[runtime] {runtime_fqn} describe failed ({e}), retrying...")
            time.sleep(15)
            continue
        status = (desc.get("status") or desc.get("STATUS")) if desc else None
        if status in target_statuses:
            print(f"[runtime] {runtime_fqn} reached status {status}")
            if "FAILED" in status:
                raise RuntimeError(f"Runtime {runtime_fqn} entered failed state: {status}")
            return status
        print(f"[runtime] {runtime_fqn} status: {status}, waiting...")
        time.sleep(15)
    raise TimeoutError(f"Runtime {runtime_fqn} did not reach {target_statuses} within {timeout}s")


def alter_runtime(name, database, schema, changed_fields, eai_names=None, **kwargs):
    print(f"[runtime] Altering runtime '{name}'...", file=sys.stderr)
    runtime_fqn = fqn(database, schema, name)
    set_clauses = []
    alterable = ("min_nodes", "max_nodes", "execute_as_role", "display_name", "comment")
    for field in alterable:
        if field in changed_fields:
            val = changed_fields[field]["new"]
            if isinstance(val, str) and field in ("display_name", "comment"):
                set_clauses.append(f"{field.upper()} = '{val}'")
            elif isinstance(val, str):
                set_clauses.append(f"{field.upper()} = {val}")
            else:
                set_clauses.append(f"{field.upper()} = {val}")
    if eai_names is not None:
        eai_list = ", ".join(eai_names)
        set_clauses.append(f"EXTERNAL_ACCESS_INTEGRATIONS = ({eai_list})")
    if set_clauses:
        sql = f"ALTER OPENFLOW RUNTIME {runtime_fqn} SET {' '.join(set_clauses)}"
        print(f"[runtime] Altering {runtime_fqn}: {', '.join(set_clauses)}")
        snow_sql(sql, **kwargs)
        print(f"[runtime] Altered {runtime_fqn}")


def suspend_runtime(name, database, schema, **kwargs):
    print(f"[runtime] Suspending runtime '{name}'...", file=sys.stderr)
    runtime_fqn = fqn(database, schema, name)
    desc = describe_runtime(name, database, schema, **kwargs)
    status = (desc.get("status") or desc.get("STATUS")) if desc else None
    if status == "SUSPENDED":
        print(f"[runtime] {runtime_fqn} already SUSPENDED")
        return
    print(f"[runtime] Suspending {runtime_fqn}...")
    snow_sql(f"ALTER OPENFLOW RUNTIME {runtime_fqn} SUSPEND", **kwargs)
    wait_runtime_status(name, database, schema, ["SUSPENDED", "SUSPEND_FAILED"], **kwargs)


def resume_runtime(name, database, schema, **kwargs):
    print(f"[runtime] Resuming runtime '{name}'...", file=sys.stderr)
    runtime_fqn = fqn(database, schema, name)
    desc = describe_runtime(name, database, schema, **kwargs)
    status = (desc.get("status") or desc.get("STATUS")) if desc else None
    if status == "ACTIVE":
        print(f"[runtime] {runtime_fqn} already ACTIVE")
        return
    print(f"[runtime] Resuming {runtime_fqn}...")
    snow_sql(f"ALTER OPENFLOW RUNTIME {runtime_fqn} RESUME", **kwargs)
    wait_runtime_status(name, database, schema, ["ACTIVE", "ACTIVATE_FAILED"], **kwargs)


def terminate_runtime(name, database, schema, **kwargs):
    print(f"[runtime] Terminating runtime '{name}'...", file=sys.stderr)
    runtime_fqn = fqn(database, schema, name)
    print(f"[runtime] Terminating {runtime_fqn}...")
    snow_sql(f"ALTER OPENFLOW RUNTIME {runtime_fqn} TERMINATE", **kwargs)
    wait_runtime_status(name, database, schema, ["DELETED", "DELETE_FAILED"], **kwargs)


def drop_runtime(name, database, schema, **kwargs):
    print(f"[runtime] Dropping runtime '{name}'...", file=sys.stderr)
    runtime_fqn = fqn(database, schema, name)
    print(f"[runtime] Dropping {runtime_fqn}...")
    snow_sql(f"DROP OPENFLOW RUNTIME IF EXISTS {runtime_fqn}", **kwargs)
    print(f"[runtime] Dropped {runtime_fqn}")


def delete_runtime(name, database, schema, **kwargs):
    print(f"[runtime] Deleting runtime '{name}' (suspend → terminate → drop)...", file=sys.stderr)
    suspend_runtime(name, database, schema, **kwargs)
    terminate_runtime(name, database, schema, **kwargs)
    drop_runtime(name, database, schema, **kwargs)


def main():
    parser = argparse.ArgumentParser(description="Manage Openflow runtimes")
    parser.add_argument("action", choices=["create", "alter", "delete", "describe"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--deployment")
    parser.add_argument("--database", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--node-type")
    parser.add_argument("--min-nodes", type=int)
    parser.add_argument("--max-nodes", type=int)
    parser.add_argument("--execute-as-role")
    parser.add_argument("--eai-names", help="Comma-separated EAI names")
    parser.add_argument("--display-name")
    parser.add_argument("--comment")
    parser.add_argument("--changed-fields", help="JSON string of changed fields")
    parser.add_argument("--account-url", required=True)
    parser.add_argument("--pat", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--role", required=True)
    args = parser.parse_args()

    conn = {"account_url": args.account_url, "pat": args.pat, "user": args.user, "role": args.role}
    eai_names = args.eai_names.split(",") if args.eai_names else None

    if args.action == "create":
        create_runtime(
            args.name, args.deployment, args.database, args.schema,
            args.node_type, args.min_nodes, args.max_nodes,
            args.execute_as_role, eai_names=eai_names,
            display_name=args.display_name, comment=args.comment, **conn
        )
    elif args.action == "alter":
        fields = json.loads(args.changed_fields) if args.changed_fields else {}
        alter_runtime(args.name, args.database, args.schema, fields,
                      eai_names=eai_names, **conn)
    elif args.action == "delete":
        delete_runtime(args.name, args.database, args.schema, **conn)
    elif args.action == "describe":
        desc = describe_runtime(args.name, args.database, args.schema, **conn)
        json.dump(desc, sys.stdout, indent=2)


if __name__ == "__main__":
    main()