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
import os
import re
import time
from collections import defaultdict

import nipyapi

from manage_flows import configure_nifi

_SECRET_RE = re.compile(r'^\$\{\{\s*secrets\.(\w+)\s*\}\}$')
_VAR_RE = re.compile(r'^\$\{\{\s*vars\.(\w+)\s*\}\}$')


def _gh_secrets():
    raw = os.environ.get("GH_SECRETS_JSON", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _gh_vars():
    raw = os.environ.get("GH_VARS_JSON", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def resolve_value(value):
    """Resolve ${{ secrets.NAME }} and ${{ vars.NAME }} references from GitHub Environment."""
    if value is None:
        return None
    s = str(value)
    m = _SECRET_RE.match(s)
    if m:
        name = m.group(1)
        resolved = _gh_secrets().get(name)
        if resolved is None:
            raise RuntimeError(f"Secret '{name}' not found in GitHub Environment (GH_SECRETS_JSON)")
        return resolved
    m = _VAR_RE.match(s)
    if m:
        name = m.group(1)
        resolved = _gh_vars().get(name)
        if resolved is None:
            raise RuntimeError(f"Variable '{name}' not found in GitHub Environment (GH_VARS_JSON)")
        return resolved
    return s


def _build_param_map(pc_id):
    """Return {param_name: (context_id, current_value)} for a PC and all inherited PCs.
    Direct PC parameters take precedence over inherited ones.
    """
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
                result[param.name] = (cid, param.value)
        for inherited in (pc.component.inherited_parameter_contexts or []):
            walk(inherited.id)

    walk(pc_id)
    return result


def _apply_context_updates(context_id, changes):
    """Merge changes into a parameter context and submit an async update request."""
    api = nipyapi.nifi.ParameterContextsApi()
    pc = api.get_parameter_context(id=context_id)

    current = {p.parameter.name: p.parameter for p in (pc.component.parameters or [])}

    updated = []
    for name, param in current.items():
        has_asset = bool(param.referenced_assets)
        if name in changes:
            if has_asset:
                print(f"[params] Skipping '{name}' — bound to asset, not overwriting with value")
                updated.append(nipyapi.nifi.ParameterEntity(parameter=param))
                continue
            updated.append(nipyapi.nifi.ParameterEntity(
                parameter=nipyapi.nifi.ParameterDTO(
                    name=name,
                    value=changes[name],
                    sensitive=param.sensitive,
                    description=param.description,
                )
            ))
            display = "***" if param.sensitive else repr(changes[name])
            print(f"[params]   '{name}' -> {display}")
        else:
            if bool(param.referenced_assets):
                updated.append(nipyapi.nifi.ParameterEntity(
                    parameter=nipyapi.nifi.ParameterDTO(
                        name=name,
                        sensitive=param.sensitive,
                        description=param.description,
                        referenced_assets=param.referenced_assets,
                    )
                ))
            else:
                updated.append(nipyapi.nifi.ParameterEntity(parameter=param))

    body = nipyapi.nifi.ParameterContextEntity(
        id=context_id,
        revision=pc.revision,
        component=nipyapi.nifi.ParameterContextDTO(
            id=context_id,
            name=pc.component.name,
            parameters=updated,
            inherited_parameter_contexts=pc.component.inherited_parameter_contexts,
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
                raise RuntimeError(f"[params] Parameter context update failed: {status.request.failure_reason}")
            break

    api.delete_update_request(context_id=context_id, request_id=request_id)
    print(f"[params] Context '{pc.component.name}' updated ({len(changes)} change(s))")


def reconcile_flow_parameters(pg_id, desired_params, pg_name=""):
    """Idempotent: set parameter values for a flow's parameter context(s).
    Parameters are specified without context — the context is resolved automatically.
    desired_params: dict of {param_name: value} where value=None clears the parameter.
    """
    print(f"[params] Reconciling parameters for '{pg_name}'...", file=sys.stderr)
    if not desired_params:
        return

    pg = nipyapi.nifi.ProcessGroupsApi().get_process_group(id=pg_id)
    pc_ref = pg.component.parameter_context
    if not pc_ref:
        print(f"[params] '{pg_name or pg_id}' has no parameter context — skipping")
        return

    param_map = _build_param_map(pc_ref.id)

    groups = defaultdict(dict)
    for param_name, new_value in desired_params.items():
        if param_name not in param_map:
            print(f"[params] WARNING: '{param_name}' not found in parameter contexts of '{pg_name or pg_id}'")
            continue
        context_id, current_value = param_map[param_name]
        resolved = resolve_value(new_value)
        new_str = str(resolved) if resolved is not None else None
        if current_value != new_str:
            groups[context_id][param_name] = new_str
        else:
            pass  # already printed below
            if not new_str:  # skip noisy "already at desired value" for intentionally-unset params
                pass
            else:
                print(f"[params] '{param_name}' already at desired value")

    if not groups:
        print(f"[params] '{pg_name or pg_id}' all parameters up-to-date")
        return

    for context_id, changes in groups.items():
        _apply_context_updates(context_id, changes)


def reconcile_parameters(flows_with_params, runtime_url, nifi_pat):
    """Reconcile parameters for all flows that declare a parameters section."""
    print(f"[params] Reconciling parameters for {len(flows_with_params)} flow(s)...", file=sys.stderr)
    configure_nifi(runtime_url, nifi_pat)
    for flow_spec, pg_id in flows_with_params:
        desired = flow_spec.get("parameters")
        if not desired:
            continue
        print(f"[params] Reconciling parameters for '{flow_spec['name']}'...")
        reconcile_flow_parameters(pg_id, desired, pg_name=flow_spec["name"])


def _find_parameter_context_by_name(name):
    api = nipyapi.nifi.FlowApi()
    pcs = api.get_parameter_contexts()
    for pc in (pcs.parameter_contexts or []):
        if pc.component.name == name:
            return pc
    return None


def add_inherited_parameter_contexts(pg_id, context_names, pg_name=""):
    print(f"[params] Adding {len(context_names)} inherited context(s) to '{pg_name}'...", file=sys.stderr)
    if not context_names:
        return
    pg = nipyapi.nifi.ProcessGroupsApi().get_process_group(id=pg_id)
    pc_ref = pg.component.parameter_context
    if not pc_ref:
        print(f"[params] '{pg_name or pg_id}' has no parameter context — cannot add inheritance")
        return

    api = nipyapi.nifi.ParameterContextsApi()
    pc = api.get_parameter_context(id=pc_ref.id)
    existing_inherited = list(pc.component.inherited_parameter_contexts or [])
    existing_ids = {ipc.id for ipc in existing_inherited}

    added = []
    for ctx_name in context_names:
        ctx = _find_parameter_context_by_name(ctx_name)
        if not ctx:
            print(f"[params] WARNING: parameter context '{ctx_name}' not found — skipping inheritance")
            continue
        if ctx.id in existing_ids:
            print(f"[params] '{ctx_name}' already inherited by '{pc.component.name}'")
            continue
        existing_inherited.append(nipyapi.nifi.ParameterContextReferenceEntity(
            id=ctx.id,
            component=nipyapi.nifi.ParameterContextReferenceDTO(id=ctx.id, name=ctx_name),
        ))
        added.append(ctx_name)

    if not added:
        print(f"[params] No new inherited contexts to add for '{pg_name or pg_id}'")
        return

    params = []
    for p in (pc.component.parameters or []):
        param = p.parameter
        if getattr(param, 'referenced_assets', None):
            param.value = None
        params.append(nipyapi.nifi.ParameterEntity(parameter=param))

    body = nipyapi.nifi.ParameterContextEntity(
        id=pc_ref.id,
        revision=pc.revision,
        component=nipyapi.nifi.ParameterContextDTO(
            id=pc_ref.id,
            name=pc.component.name,
            parameters=params,
            inherited_parameter_contexts=existing_inherited,
        ),
    )
    req = api.submit_parameter_context_update(context_id=pc_ref.id, body=body)
    request_id = req.request.request_id
    while True:
        time.sleep(1)
        status = api.get_parameter_context_update(context_id=pc_ref.id, request_id=request_id)
        if status.request.complete:
            if status.request.failure_reason:
                api.delete_update_request(context_id=pc_ref.id, request_id=request_id)
                raise RuntimeError(f"[params] Inheritance update failed: {status.request.failure_reason}")
            break
    api.delete_update_request(context_id=pc_ref.id, request_id=request_id)
    print(f"[params] Added inherited contexts {added} to '{pc.component.name}'")


def apply_parameter_overrides(pg_id, overrides, pg_name=""):
    """Add/update parameters in the flow's DIRECT parameter context to shadow inherited values."""
    print(f"[params] Applying parameter overrides to '{pg_name}'...", file=sys.stderr)
    if not overrides:
        return

    pg = nipyapi.nifi.ProcessGroupsApi().get_process_group(id=pg_id)
    pc_ref = pg.component.parameter_context
    if not pc_ref:
        print(f"[params] '{pg_name or pg_id}' has no parameter context — skipping overrides")
        return

    param_map = _build_param_map(pc_ref.id)

    for param_name, value in overrides.items():
        resolved = resolve_value(value)
        is_sensitive = False
        if param_name in param_map:
            ctx_id, _ = param_map[param_name]
            if ctx_id != pc_ref.id:
                api = nipyapi.nifi.ParameterContextsApi()
                inherited_pc = api.get_parameter_context(id=ctx_id)
                for p in (inherited_pc.component.parameters or []):
                    if p.parameter.name == param_name:
                        is_sensitive = p.parameter.sensitive
                        break

        param = nipyapi.parameters.prepare_parameter(
            name=param_name,
            value=resolved,
            sensitive=is_sensitive,
        )
        nipyapi.parameters.upsert_parameter_to_context(pc_ref.id, param)
        display = "***" if is_sensitive else repr(resolved)
        print(f"[params]   override '{param_name}' -> {display}")

    print(f"[params] Applied {len(overrides)} override(s) to direct context of '{pg_name or pg_id}'")