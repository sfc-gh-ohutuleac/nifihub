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
import hashlib
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict

import nipyapi

from manage_flows import configure_nifi, find_flow_pg_by_name


def _build_param_map(pc_id):
    api = nipyapi.nifi.ParameterContextsApi()
    result = {}
    seen = set()

    def walk(cid):
        if cid in seen:
            return
        seen.add(cid)
        pc = api.get_parameter_context(id=cid)
        for p in (pc.component.parameters or []):
            param = p.parameter
            if param.name not in result:
                result[param.name] = (cid, param)
        for inherited in (pc.component.inherited_parameter_contexts or []):
            walk(inherited.id)

    walk(pc_id)
    return result


def _get_existing_assets(context_id):
    api = nipyapi.nifi.ParameterContextsApi()
    result = api.get_assets(context_id=context_id)
    assets = {}
    for ae in (result.assets or []):
        a = ae.asset
        assets[a.name] = a
    return assets


def _download_file(url):
    print(f"[assets] Downloading {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "nifihub-cd/1.0"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    print(f"[assets] Downloaded {len(data)} bytes")
    return data


def _upload_asset(context_id, filename, data):
    api = nipyapi.nifi.ParameterContextsApi()
    result = api.create_asset(body=data, context_id=context_id, filename=filename)
    asset_id = result.asset.id
    print(f"[assets] Uploaded asset '{filename}' -> id={asset_id}")
    return result.asset


def _bind_asset_to_parameter(context_id, param_name, param_dto, asset_id, asset_name):
    api = nipyapi.nifi.ParameterContextsApi()
    pc = api.get_parameter_context(id=context_id)

    current = {p.parameter.name: p.parameter for p in (pc.component.parameters or [])}

    updated = []
    for name, param in current.items():
        if name == param_name:
            updated.append(nipyapi.nifi.ParameterEntity(
                parameter=nipyapi.nifi.ParameterDTO(
                    name=name,
                    sensitive=param.sensitive,
                    description=param.description,
                    referenced_assets=[
                        nipyapi.nifi.AssetReferenceDTO(id=asset_id, name=asset_name)
                    ],
                )
            ))
            print(f"[assets] Binding parameter '{name}' -> asset '{asset_name}' (id={asset_id})")
        else:
            updated.append(nipyapi.nifi.ParameterEntity(parameter=param))

    body = nipyapi.nifi.ParameterContextEntity(
        id=context_id,
        revision=pc.revision,
        component=nipyapi.nifi.ParameterContextDTO(
            id=context_id,
            name=pc.component.name,
            parameters=updated,
        ),
    )

    req = api.submit_parameter_context_update(context_id=context_id, body=body)
    request_id = req.request.request_id

    while True:
        time.sleep(1)
        status = api.get_parameter_context_update(context_id=context_id, request_id=request_id)
        if status.request.complete:
            if status.request.failure_reason:
                api.delete_update_request(context_id=context_id, request_id=request_id)
                raise RuntimeError(f"[assets] Parameter context update failed: {status.request.failure_reason}")
            break

    api.delete_update_request(context_id=context_id, request_id=request_id)
    print(f"[assets] Parameter '{param_name}' bound to asset '{asset_name}'")


def reconcile_flow_assets(pg_id, assets_spec, pg_name=""):
    print(f"[assets] Reconciling assets for '{pg_name}'...", file=sys.stderr)
    if not assets_spec:
        return

    pg = nipyapi.nifi.ProcessGroupsApi().get_process_group(id=pg_id)
    pc_ref = pg.component.parameter_context
    if not pc_ref:
        print(f"[assets] '{pg_name or pg_id}' has no parameter context — skipping")
        return

    param_map = _build_param_map(pc_ref.id)

    for asset_spec in assets_spec:
        asset_name = asset_spec["name"]
        asset_url = asset_spec["url"]
        param_name = asset_spec["parameter"]

        if param_name not in param_map:
            print(f"[assets] WARNING: parameter '{param_name}' not found in context of '{pg_name}' — skipping asset '{asset_name}'")
            continue

        context_id, param_dto = param_map[param_name]

        existing = _get_existing_assets(context_id)
        if asset_name in existing:
            existing_asset = existing[asset_name]
            if not existing_asset.missing_content:
                print(f"[assets] Asset '{asset_name}' already exists (id={existing_asset.id}) — skipping upload")
                already_bound = False
                if param_dto.referenced_assets:
                    for ref in param_dto.referenced_assets:
                        if ref.id == existing_asset.id or ref.name == asset_name:
                            already_bound = True
                            break
                if already_bound:
                    print(f"[assets] Parameter '{param_name}' already bound to asset '{asset_name}'")
                    continue
                _bind_asset_to_parameter(context_id, param_name, param_dto, existing_asset.id, asset_name)
                continue

        data = _download_file(asset_url)
        asset = _upload_asset(context_id, asset_name, data)
        _bind_asset_to_parameter(context_id, param_name, param_dto, asset.id, asset_name)


def reconcile_assets(flows_with_assets, runtime_url, nifi_pat):
    print(f"[assets] Reconciling assets for {len(flows_with_assets)} flow(s)...", file=sys.stderr)
    configure_nifi(runtime_url, nifi_pat)
    for flow_spec, pg_id in flows_with_assets:
        assets = flow_spec.get("assets")
        if not assets:
            continue
        print(f"[assets] Reconciling assets for '{flow_spec['name']}'...")
        reconcile_flow_assets(pg_id, assets, pg_name=flow_spec["name"])