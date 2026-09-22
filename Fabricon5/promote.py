"""Promote Fabric workspace content: refresh Dev from git, deploy Dev -> Prod,
fix stage bindings, verify, refresh semantic models.

Phases:
  1. gate    - refuse if the Dev workspace has uncommitted changes
  2. update  - updateFromGit so Dev matches the branch
  3. deploy  - deployment pipeline Dev -> Prod
  4. bind    - re-point notebook default lakehouses at the target stage
               (ported from the DevOps notebooks; errors fail the run here
               instead of being printed and skipped)
  5. verify  - assert no target item still references a source-stage id
  6. refresh - refresh deployed semantic models so calculated/import tables hold data

Auth: FABRIC_TOKEN env var (bearer for https://api.fabric.microsoft.com).
Semantic model refresh uses the same token against the Power BI API.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

FABRIC = "https://api.fabric.microsoft.com/v1"
POWERBI = "https://api.powerbi.com/v1.0/myorg"

# Variable libraries whose active value set the verify phase should assert,
# e.g. VERIFY_ACTIVE_VALUE_SETS="Config=Prod" (comma-separated for several).
EXPECTED_ACTIVE_VALUE_SET = dict(
    pair.split("=", 1)
    for pair in os.environ.get("VERIFY_ACTIVE_VALUE_SETS", "").split(",")
    if "=" in pair
)


def call(method, url, body=None, ok=(200, 201, 202)):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + os.environ["FABRIC_TOKEN"],
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            if resp.status not in ok:
                raise SystemExit(f"{method} {url}: unexpected status {resp.status}")
            return resp.status, resp.headers, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {url} failed: {e.code} {e.read().decode(errors='replace')}")


def wait_for_operation(headers, description):
    op_id = headers.get("x-ms-operation-id")
    if not op_id:
        return None
    while True:
        _, _, op = call("GET", f"{FABRIC}/operations/{op_id}")
        state = op.get("status")
        if state == "Succeeded":
            print(f"{description}: succeeded")
            return op_id
        if state in ("Failed", "Cancelled"):
            raise SystemExit(f"{description}: {state}: {op.get('error')}")
        time.sleep(10)


def get_definition(workspace, item, fmt=None):
    suffix = f"?format={fmt}" if fmt else ""
    status, headers, body = call(
        "POST", f"{FABRIC}/workspaces/{workspace}/items/{item}/getDefinition{suffix}"
    )
    if status == 202:
        op_id = wait_for_operation(headers, "getDefinition")
        _, _, body = call("GET", f"{FABRIC}/operations/{op_id}/result")
    return body["definition"]


def update_definition(workspace, item, definition):
    _, headers, _ = call(
        "POST",
        f"{FABRIC}/workspaces/{workspace}/items/{item}/updateDefinition",
        {"definition": definition},
    )
    wait_for_operation(headers, "updateDefinition")


def list_items(workspace):
    _, _, body = call("GET", f"{FABRIC}/workspaces/{workspace}/items")
    return body["value"]


def gate_and_update(workspace):
    _, _, status = call("GET", f"{FABRIC}/workspaces/{workspace}/git/status")
    dirty = sorted(
        c["itemMetadata"]["displayName"]
        for c in status.get("changes", [])
        if c.get("workspaceChange")
    )
    if dirty:
        raise SystemExit(
            "Dev workspace has uncommitted changes, refusing to promote: " + ", ".join(dirty)
        )
    head, remote = status.get("workspaceHead"), status.get("remoteCommitHash")
    if head != remote:
        print(f"updating Dev workspace from git: {head} -> {remote}")
        _, headers, _ = call(
            "POST",
            f"{FABRIC}/workspaces/{workspace}/git/updateFromGit",
            {
                "workspaceHead": head,
                "remoteCommitHash": remote,
                "conflictResolution": {
                    "conflictResolutionType": "Workspace",
                    "conflictResolutionPolicy": "PreferRemote",
                },
                "options": {"allowOverrideItems": True},
            },
        )
        wait_for_operation(headers, "updateFromGit")
    else:
        print("Dev workspace already matches the remote head")
    return remote


def deploy(pipeline, source_stage, target_stage, note):
    _, headers, _ = call(
        "POST",
        f"{FABRIC}/deploymentPipelines/{pipeline}/deploy",
        {"sourceStageId": source_stage, "targetStageId": target_stage, "note": note},
    )
    wait_for_operation(headers, "deploy")


def bind_notebook_lakehouses(target_workspace, items):
    """Point each notebook's default lakehouse at the same-named lakehouse in the
    target workspace. Ported from the DevOps notebooks; a failure here fails the
    promotion instead of leaving a half-bound stage."""
    lakehouses = {i["displayName"]: i["id"] for i in items if i["type"] == "Lakehouse"}
    for nb in (i for i in items if i["type"] == "Notebook"):
        definition = get_definition(target_workspace, nb["id"], fmt="ipynb")
        part = next(p for p in definition["parts"] if p["path"].endswith(".ipynb"))
        content = json.loads(base64.b64decode(part["payload"]))
        dep = content.get("metadata", {}).get("dependencies", {}).get("lakehouse")
        if not dep or not dep.get("default_lakehouse_name"):
            print(f"bind: {nb['displayName']}: no default lakehouse declared, skipping")
            continue
        name = dep["default_lakehouse_name"]
        if name not in lakehouses:
            raise SystemExit(
                f"bind: {nb['displayName']} declares default lakehouse '{name}' "
                f"which does not exist in the target workspace"
            )
        if (
            dep.get("default_lakehouse") == lakehouses[name]
            and dep.get("default_lakehouse_workspace_id") == target_workspace
        ):
            print(f"bind: {nb['displayName']}: already bound to target '{name}'")
            continue
        dep["default_lakehouse"] = lakehouses[name]
        dep["default_lakehouse_workspace_id"] = target_workspace
        part["payload"] = base64.b64encode(json.dumps(content).encode()).decode()
        update_definition(target_workspace, nb["id"], definition)
        print(f"bind: {nb['displayName']}: default lakehouse -> '{name}' in target workspace")


def endpoint_map(source_workspace, target_workspace):
    """Map source-stage SQL endpoint identities to target-stage ones, matching
    lakehouses by name. Used to rebind Direct Lake semantic models."""
    def endpoints(ws):
        out = {}
        for lh in (i for i in list_items(ws) if i["type"] == "Lakehouse"):
            _, _, detail = call("GET", f"{FABRIC}/workspaces/{ws}/lakehouses/{lh['id']}")
            props = detail.get("properties", {}).get("sqlEndpointProperties") or {}
            if props.get("connectionString"):
                out[lh["displayName"]] = (props["connectionString"], props["id"])
        return out
    src, tgt = endpoints(source_workspace), endpoints(target_workspace)
    pairs = {}
    for name, (conn, db) in src.items():
        if name in tgt:
            pairs[conn] = tgt[name][0]
            pairs[db] = tgt[name][1]
    return pairs


def bind_semantic_models(source_workspace, target_workspace, items):
    """Direct Lake models do not autobind: rewrite any source-stage SQL endpoint
    reference in a target model to the same-named lakehouse's endpoint in the
    target stage."""
    pairs = endpoint_map(source_workspace, target_workspace)
    if not pairs:
        return
    for sm in (i for i in items if i["type"] == "SemanticModel"):
        definition = get_definition(target_workspace, sm["id"])
        changed = 0
        for part in definition["parts"]:
            text = base64.b64decode(part["payload"]).decode("utf-8", errors="replace")
            new = text
            for old, replacement in pairs.items():
                new = new.replace(old, replacement)
            if new != text:
                part["payload"] = base64.b64encode(new.encode()).decode()
                changed += 1
        if changed:
            update_definition(target_workspace, sm["id"], definition)
            print(f"bind: {sm['displayName']}: rebound to target-stage SQL endpoint")
        else:
            print(f"bind: {sm['displayName']}: no source-stage endpoint references")


def verify(source_workspace, target_workspace, items):
    """No target item may still reference a source-stage identity."""
    source_ids = {i["id"] for i in list_items(source_workspace)} | {source_workspace}
    failures = []
    for nb in (i for i in items if i["type"] == "Notebook"):
        definition = get_definition(target_workspace, nb["id"], fmt="ipynb")
        part = next(p for p in definition["parts"] if p["path"].endswith(".ipynb"))
        dep = (
            json.loads(base64.b64decode(part["payload"]))
            .get("metadata", {}).get("dependencies", {}).get("lakehouse") or {}
        )
        for key in ("default_lakehouse", "default_lakehouse_workspace_id"):
            if dep.get(key) in source_ids:
                failures.append(f"{nb['displayName']}: {key} references the source stage")
    for pl in (i for i in items if i["type"] == "DataPipeline"):
        definition = get_definition(target_workspace, pl["id"])
        for part in definition["parts"]:
            text = base64.b64decode(part["payload"]).decode(errors="replace")
            for sid in source_ids:
                if sid in text:
                    failures.append(f"{pl['displayName']}: {part['path']} references {sid}")
    for rp in (i for i in items if i["type"] == "Report"):
        definition = get_definition(target_workspace, rp["id"])
        for part in definition["parts"]:
            if part["path"] == "definition.pbir":
                text = base64.b64decode(part["payload"]).decode(errors="replace")
                for sid in source_ids:
                    if sid in text:
                        failures.append(f"{rp['displayName']}: bound to a source-stage model")
    source_endpoints = set(endpoint_map(source_workspace, target_workspace).keys())
    for sm in (i for i in items if i["type"] == "SemanticModel"):
        definition = get_definition(target_workspace, sm["id"])
        for part in definition["parts"]:
            text = base64.b64decode(part["payload"]).decode(errors="replace")
            for marker in source_ids | source_endpoints:
                if marker in text:
                    failures.append(f"{sm['displayName']}: {part['path']} references the source stage")
    for vl in (i for i in items if i["type"] == "VariableLibrary"):
        expected = EXPECTED_ACTIVE_VALUE_SET.get(vl["displayName"])
        if expected:
            _, _, detail = call(
                "GET", f"{FABRIC}/workspaces/{target_workspace}/VariableLibraries/{vl['id']}"
            )
            active = detail.get("properties", {}).get("activeValueSetName")
            if active != expected:
                failures.append(
                    f"{vl['displayName']}: active value set is '{active}', expected '{expected}'"
                )
    if failures:
        raise SystemExit("verification failed:\n  " + "\n  ".join(failures))
    print(f"verify: {len(items)} target items checked, no source-stage references")


def refresh_semantic_models(target_workspace, items):
    for sm in (i for i in items if i["type"] == "SemanticModel"):
        call(
            "POST",
            f"{POWERBI}/groups/{target_workspace}/datasets/{sm['id']}/refreshes",
            {"type": "full", "commitMode": "transactional"},
        )
        print(f"refresh: {sm['displayName']}: triggered")
    for sm in (i for i in items if i["type"] == "SemanticModel"):
        while True:
            _, _, body = call(
                "GET", f"{POWERBI}/groups/{target_workspace}/datasets/{sm['id']}/refreshes?$top=1"
            )
            status = body["value"][0]["status"] if body.get("value") else "Unknown"
            if status == "Completed":
                print(f"refresh: {sm['displayName']}: completed")
                break
            if status == "Failed":
                raise SystemExit(f"refresh: {sm['displayName']}: failed")
            time.sleep(10)


def cleanup_feature_workspaces():
    """Delete branched-out feature workspaces whose git branch is gone (merged
    and deleted). Guardrails: only workspaces whose name starts with
    FEATURE_WS_PREFIX, and only when the connected branch no longer exists in
    the repo. A workspace with a live branch is active work and is left alone."""
    prefix = os.environ.get("FEATURE_WS_PREFIX")
    ado_token = os.environ.get("ADO_TOKEN")
    if not prefix or not ado_token:
        print("cleanup: FEATURE_WS_PREFIX or ADO_TOKEN not set, skipping")
        return
    org = os.environ["ADO_ORG_URL"].rstrip("/")
    project = os.environ["ADO_PROJECT"]
    repo = os.environ["ADO_REPO"]

    def branch_exists(name):
        req = urllib.request.Request(
            f"{org}/{project}/_apis/git/repositories/{repo}/refs"
            f"?filter=heads/{urllib.parse.quote(name)}&api-version=7.1",
            headers={"Authorization": "Bearer " + ado_token},
        )
        with urllib.request.urlopen(req) as resp:
            refs = json.loads(resp.read()).get("value", [])
        return any(r.get("name") == f"refs/heads/{name}" for r in refs)

    _, _, body = call("GET", f"{FABRIC}/workspaces")
    for ws in body["value"]:
        if not ws["displayName"].startswith(prefix):
            continue
        try:
            _, _, conn = call("GET", f"{FABRIC}/workspaces/{ws['id']}/git/connection")
        except SystemExit:
            print(f"cleanup: {ws['displayName']}: no readable git connection, leaving alone")
            continue
        details = conn.get("gitProviderDetails") or {}
        branch = details.get("branchName")
        if not branch:
            print(f"cleanup: {ws['displayName']}: not git-connected, leaving alone")
            continue
        if branch_exists(branch):
            print(f"cleanup: {ws['displayName']}: branch '{branch}' still exists, active work")
            continue
        call("DELETE", f"{FABRIC}/workspaces/{ws['id']}", ok=(200, 204))
        print(f"cleanup: {ws['displayName']}: branch '{branch}' is gone, workspace deleted")


def main():
    dev = os.environ["DEV_WORKSPACE_ID"]
    prod = os.environ["PROD_WORKSPACE_ID"]
    pipeline = os.environ["DEPLOYMENT_PIPELINE_ID"]
    source_stage = os.environ["DEV_STAGE_ID"]
    target_stage = os.environ["PROD_STAGE_ID"]

    remote = gate_and_update(dev)
    deploy(pipeline, source_stage, target_stage, f"Automated deploy of {remote[:8]}")
    items = list_items(prod)
    bind_notebook_lakehouses(prod, items)
    bind_semantic_models(dev, prod, items)
    verify(dev, prod, items)
    refresh_semantic_models(prod, items)
    cleanup_feature_workspaces()
    print("promotion complete")


if __name__ == "__main__":
    main()
