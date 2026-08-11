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


def create_deployment(name, deployment_type, display_name=None, comment=None, **kwargs):
    print(f"[deployment] Creating deployment '{name}'...", file=sys.stderr)
    sql = f"CREATE OPENFLOW DEPLOYMENT {name} DEPLOYMENT_TYPE = {deployment_type}"
    if display_name:
        sql += f" DISPLAY_NAME = '{display_name}'"
    if comment:
        sql += f" COMMENT = '{comment}'"
    print(f"[deployment] Creating {name}...")
    try:
        snow_sql(sql, timeout=900, **kwargs)
    except (subprocess.TimeoutExpired, TimeoutError):
        print(f"[deployment] CREATE command timed out, polling for status...")
    except RuntimeError as e:
        err = str(e).lower()
        if "already exists" in err:
            print(f"[deployment] {name} already exists — skipping create")
            return
        if "timed out" in err:
            print(f"[deployment] CREATE command timed out, polling for status...")
        else:
            raise
    print(f"[deployment] Waiting for {name} to become ACTIVE...")
    wait_deployment_status(name, ["ACTIVE", "CREATE_FAILED"], timeout=1200, **kwargs)


def alter_deployment(name, changed_fields, **kwargs):
    print(f"[deployment] Altering deployment '{name}'...", file=sys.stderr)
    set_clauses = []
    for field in ("display_name", "comment"):
        if field in changed_fields:
            val = changed_fields[field]["new"]
            if val is not None:
                set_clauses.append(f"{field.upper()} = '{val}'")
    if set_clauses:
        sql = f"ALTER OPENFLOW DEPLOYMENT {name} SET {' '.join(set_clauses)}"
        print(f"[deployment] Altering {name}: {', '.join(set_clauses)}")
        snow_sql(sql, **kwargs)
        print(f"[deployment] Altered {name}")


def describe_deployment(name, **kwargs):
    rows = snow_sql(f"DESCRIBE OPENFLOW DEPLOYMENT {name}", **kwargs)
    if rows:
        return rows[0] if isinstance(rows, list) else rows
    return None


def wait_deployment_status(name, target_statuses, timeout=600, **kwargs):
    start = time.time()
    while time.time() - start < timeout:
        try:
            desc = describe_deployment(name, **kwargs)
        except Exception as e:
            print(f"[deployment] {name} describe failed ({e}), retrying...")
            time.sleep(10)
            continue
        status = (desc.get("status") or desc.get("STATUS")) if desc else None
        if status in target_statuses:
            print(f"[deployment] {name} reached status {status}")
            if "FAILED" in status:
                raise RuntimeError(f"Deployment {name} entered failed state: {status}")
            return status
        print(f"[deployment] {name} status: {status}, waiting...")
        time.sleep(10)
    raise TimeoutError(f"Deployment {name} did not reach {target_statuses} within {timeout}s")


def terminate_deployment(name, **kwargs):
    print(f"[deployment] Terminating {name}...")
    snow_sql(f"ALTER OPENFLOW DEPLOYMENT {name} TERMINATE", **kwargs)
    wait_deployment_status(name, ["DELETED"], **kwargs)
    print(f"[deployment] Terminated {name}")


def drop_deployment(name, **kwargs):
    print(f"[deployment] Dropping {name}...")
    snow_sql(f"DROP OPENFLOW DEPLOYMENT IF EXISTS {name}", **kwargs)
    print(f"[deployment] Dropped {name}")


def delete_deployment(name, **kwargs):
    print(f"[deployment] Deleting deployment '{name}'...", file=sys.stderr)
    terminate_deployment(name, **kwargs)
    drop_deployment(name, **kwargs)


def main():
    parser = argparse.ArgumentParser(description="Manage Openflow deployments")
    parser.add_argument("action", choices=["create", "alter", "delete", "describe"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--deployment-type", default="SNOWFLAKE")
    parser.add_argument("--display-name")
    parser.add_argument("--comment")
    parser.add_argument("--changed-fields", help="JSON string of changed fields")
    parser.add_argument("--account-url", required=True)
    parser.add_argument("--pat", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--role", required=True)
    args = parser.parse_args()

    conn = {"account_url": args.account_url, "pat": args.pat, "user": args.user, "role": args.role}

    if args.action == "create":
        create_deployment(
            args.name, args.deployment_type,
            display_name=args.display_name, comment=args.comment, **conn
        )
    elif args.action == "alter":
        fields = json.loads(args.changed_fields) if args.changed_fields else {}
        alter_deployment(args.name, fields, **conn)
    elif args.action == "delete":
        delete_deployment(args.name, **conn)
    elif args.action == "describe":
        desc = describe_deployment(args.name, **conn)
        json.dump(desc, sys.stdout, indent=2)


if __name__ == "__main__":
    main()