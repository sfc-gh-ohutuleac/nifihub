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
import json
import sys
import os
import tempfile
import time

from manage_deployment import snow_sql


def fqn(database, schema, name):
    return f"{database}.{schema}.{name}"


def _get_status(desc):
    if not desc:
        return None
    return desc.get("status") or desc.get("STATUS")


def describe_connector(name, database, schema, **kwargs):
    connector_fqn = fqn(database, schema, name)
    rows = snow_sql(f"DESCRIBE OPENFLOW CONNECTOR {connector_fqn}", **kwargs)
    if rows:
        return rows[0] if isinstance(rows, list) else rows
    return None


def create_connector(name, runtime_name, database, schema, definition,
                     display_name=None, comment=None, **kwargs):
    print(f"[connector] Creating connector '{name}' on runtime '{runtime_name}'...", file=sys.stderr)
    connector_fqn = fqn(database, schema, name)
    runtime_fqn = fqn(database, schema, runtime_name)
    sql = (
        f"CREATE OPENFLOW CONNECTOR {connector_fqn} "
        f"IN RUNTIME {runtime_fqn} "
        f"FROM DEFINITION {definition}"
    )
    if display_name:
        sql += f" DISPLAY_NAME = '{display_name}'"
    if comment:
        sql += f" COMMENT = '{comment}'"
    print(f"[connector] Creating {connector_fqn}...")
    snow_sql(sql, **kwargs)
    print(f"[connector] Created {connector_fqn}")


def connector_exists(name, database, schema, **kwargs):
    try:
        desc = describe_connector(name, database, schema, **kwargs)
        return desc is not None
    except Exception:
        return False


def get_connector_config_uri(name, database, schema, **kwargs):
    desc = describe_connector(name, database, schema, **kwargs)
    if not desc:
        return None
    return (desc.get("live_version_location_uri")
            or desc.get("LIVE_VERSION_LOCATION_URI"))


def get_connector_config(name, database, schema, **kwargs):
    uri = get_connector_config_uri(name, database, schema, **kwargs)
    if not uri:
        print(f"[connector] No live config URI for {name}")
        return None
    uri = uri.rstrip("/")
    tmp_dir = tempfile.mkdtemp()
    try:
        snow_sql(f"GET '{uri}/config.json' 'file://{tmp_dir}/'", **kwargs)
        local_path = os.path.join(tmp_dir, "config.json")
        if os.path.exists(local_path):
            with open(local_path) as f:
                return json.load(f)
        print(f"[connector] GET did not produce config.json for {name}")
        return None
    finally:
        config_file = os.path.join(tmp_dir, "config.json")
        if os.path.exists(config_file):
            os.unlink(config_file)
        os.rmdir(tmp_dir)


def put_connector_config(name, database, schema, config_json, **kwargs):
    uri = get_connector_config_uri(name, database, schema, **kwargs)
    if not uri:
        raise RuntimeError(f"No live config URI for connector {name}")
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "config.json")
    with open(tmp_path, "w") as f:
        json.dump(config_json, f, indent=4)
    try:
        snow_sql(
            f"PUT 'file://{tmp_path}' '{uri}' AUTO_COMPRESS=FALSE OVERWRITE=TRUE", **kwargs
        )
        print(f"[connector] Uploaded config.json to {uri}")
    finally:
        os.unlink(tmp_path)
        os.rmdir(tmp_dir)


def upload_connector_asset(name, database, schema, asset_local_path, asset_filename, **kwargs):
    uri = get_connector_config_uri(name, database, schema, **kwargs)
    if not uri:
        raise RuntimeError(f"No live config URI for connector {name}")
    snow_sql(
        f"PUT 'file://{asset_local_path}' '{uri}' AUTO_COMPRESS=FALSE OVERWRITE=TRUE", **kwargs
    )
    print(f"[connector] Uploaded asset {asset_filename} to {uri}")


def add_live_version(name, database, schema, **kwargs):
    connector_fqn = fqn(database, schema, name)
    print(f"[connector] Adding live version from last for {connector_fqn}...")
    snow_sql(f"ALTER OPENFLOW CONNECTOR {connector_fqn} ADD LIVE VERSION FROM LAST", **kwargs)
    print(f"[connector] Live version created for {connector_fqn}")


def commit_connector(name, database, schema, **kwargs):
    connector_fqn = fqn(database, schema, name)
    print(f"[connector] Committing {connector_fqn}...")
    snow_sql(f"ALTER OPENFLOW CONNECTOR {connector_fqn} COMMIT", **kwargs)
    print(f"[connector] Committed {connector_fqn}")


def wait_for_connector(name, database, schema, target_status, timeout=600, **kwargs):
    connector_fqn = fqn(database, schema, name)
    print(f"[connector] Waiting for {connector_fqn} to reach {target_status}...")
    snow_sql(
        f"SELECT SYSTEM$WAIT_FOR_OPENFLOW_CONNECTORS({timeout}, '{target_status}', '{connector_fqn}')",
        timeout=timeout + 60, **kwargs
    )
    time.sleep(30)
    for attempt in range(6):
        desc = describe_connector(name, database, schema, **kwargs)
        status = _get_status(desc)
        if status == target_status:
            print(f"[connector] {connector_fqn} is now {target_status}")
            return
        print(f"[connector] {connector_fqn} still {status}, waiting... (attempt {attempt + 1}/6)")
        time.sleep(30)
    desc = describe_connector(name, database, schema, **kwargs)
    status = _get_status(desc)
    if status != target_status:
        raise RuntimeError(f"Connector {connector_fqn} did not reach {target_status} (current: {status})")


def start_connector(name, database, schema, **kwargs):
    print(f"[connector] Starting connector '{name}'...", file=sys.stderr)
    connector_fqn = fqn(database, schema, name)
    desc = describe_connector(name, database, schema, **kwargs)
    status = _get_status(desc)
    if status == "RUNNING":
        print(f"[connector] {connector_fqn} already RUNNING")
        return
    print(f"[connector] Starting {connector_fqn}...")
    snow_sql(f"ALTER OPENFLOW CONNECTOR {connector_fqn} START", **kwargs)
    wait_for_connector(name, database, schema, "RUNNING", **kwargs)


def stop_connector(name, database, schema, **kwargs):
    print(f"[connector] Stopping connector '{name}'...", file=sys.stderr)
    connector_fqn = fqn(database, schema, name)
    desc = describe_connector(name, database, schema, **kwargs)
    status = _get_status(desc)
    if status == "STOPPED":
        print(f"[connector] {connector_fqn} already STOPPED")
        return
    if status in ("START_FAILED", "DELETED"):
        print(f"[connector] {connector_fqn} is {status}, treating as stopped")
        return
    if status in ("STARTING", "CREATING", "STOPPING"):
        print(f"[connector] {connector_fqn} is {status}, waiting for stable state...")
        time.sleep(30)
        desc = describe_connector(name, database, schema, **kwargs)
        status = _get_status(desc)
        if status == "STOPPED":
            return
    print(f"[connector] Stopping {connector_fqn}...")
    snow_sql(f"ALTER OPENFLOW CONNECTOR {connector_fqn} STOP", **kwargs)
    wait_for_connector(name, database, schema, "STOPPED", **kwargs)


def delete_connector(name, database, schema, **kwargs):
    print(f"[connector] Deleting connector '{name}'...", file=sys.stderr)
    connector_fqn = fqn(database, schema, name)
    print(f"[connector] Terminating {connector_fqn}...")
    snow_sql(f"ALTER OPENFLOW CONNECTOR {connector_fqn} TERMINATE", **kwargs)
    wait_for_connector(name, database, schema, "DELETED", **kwargs)
    print(f"[connector] Dropping {connector_fqn}...")
    snow_sql(f"DROP OPENFLOW CONNECTOR IF EXISTS {connector_fqn}", **kwargs)
    print(f"[connector] Dropped {connector_fqn}")


def apply_connector_config(config_json, parameters):
    if not parameters:
        return config_json

    prop_map = {}
    for section in config_json.get("configuration", []):
        for prop_name, prop_obj in section.get("properties", {}).items():
            prop_map[prop_name] = prop_obj

    for param_name, param_value in parameters.items():
        if param_name not in prop_map:
            print(f"[connector] WARNING: parameter '{param_name}' not found in connector config — skipping")
            continue
        prop = prop_map[param_name]
        vtype = prop.get("valueType", "STRING_LITERAL")
        if vtype == "STRING_LITERAL":
            prop["value"] = param_value
        elif vtype == "SECRET_REFERENCE":
            provider = prop.get("providerName", "")
            if provider and not param_value.startswith(provider):
                prop["fullyQualifiedSecretName"] = f"{provider}.{param_value}"
            else:
                prop["fullyQualifiedSecretName"] = param_value
        elif vtype == "ASSET_REFERENCE":
            prop["assetIds"] = [param_value] if param_value else None
        else:
            prop["value"] = param_value

    return config_json


def download_asset(url, dest_path):
    import urllib.request
    print(f"[connector] Downloading asset from {url}...")
    urllib.request.urlretrieve(url, dest_path)
    print(f"[connector] Downloaded to {dest_path}")
    return dest_path