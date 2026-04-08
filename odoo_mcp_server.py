#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastmcp>=2.0",
#     "httpx>=0.27",
# ]
# ///
"""Standalone MCP server exposing Odoo operations as tools.

Run with:
    uv run odoo_mcp_server.py

Or configure in your MCP client (e.g. Claude Code, Cursor, OdooCLI):
    mcp_servers:
      odoo:
        command: uv
        args: [run, odoo_mcp_server.py]
        env:
          ODOO_URL: "https://your-instance.odoo.com"
          ODOO_DB: "your-database"
          ODOO_USER: "admin"
          ODOO_PASSWORD: "your-password"

Environment variables:
    ODOO_URL       — Odoo instance URL           (required)
    ODOO_DB        — Database name                (required)
    ODOO_USER      — Login username               (required)
    ODOO_PASSWORD  — Password for Odoo 17-18      (one of password/api_key required)
    ODOO_API_KEY   — API key for Odoo 19+         (preferred when available)
    ODOO_READONLY  — Set to "1" or "true" to disable all write operations
"""

from __future__ import annotations

import ast
import json
import logging
import os
from typing import Any, Optional

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Odoo client (self-contained — no imports from the rest of the project)
# ---------------------------------------------------------------------------


class OdooConnectionError(Exception):
    pass


class OdooClient:
    """Lightweight Odoo client supporting JSON-RPC (v17-18) and JSON-2 (v19+)."""

    def __init__(
        self,
        url: str,
        database: str,
        username: str,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.url = url.rstrip("/")
        self.database = database
        self.username = username
        self.password = password
        self.api_key = api_key
        self.uid: Optional[int] = None
        self.version: Optional[str] = None
        self._http = httpx.Client(timeout=30.0)

    # -- auth ----------------------------------------------------------------

    def authenticate(self) -> int:
        self.version = self._detect_version()

        if self.api_key and self._is_v19_plus():
            self.uid = self._auth_json2()
        else:
            self.uid = self._auth_jsonrpc()

        if not self.uid:
            raise OdooConnectionError(
                f"Authentication failed for {self.username}@{self.url}/{self.database}"
            )
        return self.uid

    def _detect_version(self) -> str:
        try:
            resp = self._jsonrpc(
                f"{self.url}/jsonrpc", "call",
                service="common", method="version", args=[],
            )
            return resp.get("server_version", "unknown")
        except Exception:
            return "unknown"

    def _is_v19_plus(self) -> bool:
        if not self.version or self.version == "unknown":
            return False
        try:
            return int(self.version.split(".")[0]) >= 19
        except (ValueError, IndexError):
            return False

    def _auth_jsonrpc(self) -> Optional[int]:
        try:
            result = self._jsonrpc(
                f"{self.url}/jsonrpc", "call",
                service="common", method="authenticate",
                args=[self.database, self.username, self.password or "", {}],
            )
            return result if isinstance(result, int) else None
        except Exception as exc:
            logger.error("JSON-RPC auth failed: %s", exc)
            return None

    def _auth_json2(self) -> Optional[int]:
        try:
            resp = self._http.get(
                f"{self.url}/api/res.users/whoami",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("uid") or data.get("id")
        except Exception as exc:
            logger.error("JSON-2 auth failed: %s", exc)
            return None

    # -- execute -------------------------------------------------------------

    def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if self.uid is None:
            raise OdooConnectionError("Not authenticated.")
        if self._is_v19_plus() and self.api_key:
            return self._exec_json2(model, method, *args, **kwargs)
        return self._exec_jsonrpc(model, method, *args, **kwargs)

    def _exec_jsonrpc(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        return self._jsonrpc(
            f"{self.url}/jsonrpc", "call",
            service="object", method="execute_kw",
            args=[self.database, self.uid, self.password or "",
                  model, method, list(args), kwargs],
        )

    def _exec_json2(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        payload: dict[str, Any] = {}
        if args:
            payload["args"] = list(args)
        if kwargs:
            payload.update(kwargs)
        resp = self._http.post(
            f"{self.url}/api/{model}/{method}",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        resp.raise_for_status()
        return resp.json()

    # -- convenience ---------------------------------------------------------

    def search_read(
        self, model: str, domain: list | None = None,
        fields: list[str] | None = None, limit: int = 2000,
        offset: int = 0, order: str | None = None,
    ) -> list[dict]:
        kw: dict[str, Any] = {"limit": limit, "offset": offset}
        if fields:
            kw["fields"] = fields
        if order:
            kw["order"] = order
        return self.execute(model, "search_read", domain or [], **kw)

    def search_count(self, model: str, domain: list | None = None) -> int:
        return self.execute(model, "search_count", domain or [])

    def create(self, model: str, values: dict) -> int:
        return self.execute(model, "create", values)

    def write(self, model: str, ids: list[int], values: dict) -> bool:
        return self.execute(model, "write", ids, values)

    def unlink(self, model: str, ids: list[int]) -> bool:
        return self.execute(model, "unlink", ids)

    # -- transport -----------------------------------------------------------

    def _jsonrpc(self, url: str, rpc_method: str, **params: Any) -> Any:
        payload = {"jsonrpc": "2.0", "method": rpc_method, "params": params, "id": 1}
        resp = self._http.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            err = body["error"]
            msg = err.get("data", {}).get("message", err.get("message", str(err)))
            raise OdooConnectionError(f"Odoo error: {msg}")
        return body.get("result")

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------------
# Connect on startup
# ---------------------------------------------------------------------------

def _connect_from_env() -> OdooClient:
    url = os.environ.get("ODOO_URL", "")
    db = os.environ.get("ODOO_DB", "")
    user = os.environ.get("ODOO_USER", "")
    password = os.environ.get("ODOO_PASSWORD")
    api_key = os.environ.get("ODOO_API_KEY")

    if not all([url, db, user]):
        raise SystemExit(
            "Set ODOO_URL, ODOO_DB, and ODOO_USER environment variables.\n"
            "Also set ODOO_PASSWORD (v17-18) or ODOO_API_KEY (v19+)."
        )
    if not password and not api_key:
        raise SystemExit(
            "Set ODOO_PASSWORD (for Odoo 17-18) or ODOO_API_KEY (for Odoo 19+)."
        )

    client = OdooClient(url=url, database=db, username=user,
                        password=password, api_key=api_key)
    client.authenticate()
    return client


odoo: OdooClient  # set at startup
READONLY: bool = os.environ.get("ODOO_READONLY", "").lower() in ("1", "true", "yes")

_READONLY_ERROR = json.dumps({
    "error": "Write operations are disabled. The server is running in read-only mode (ODOO_READONLY=true)."
})


def _check_writable() -> None:
    """Raise if the server is in read-only mode."""
    if READONLY:
        raise PermissionError(_READONLY_ERROR)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("odoo")


# -- Tools -------------------------------------------------------------------

@mcp.tool()
def odoo_search_read(
    model: str,
    domain: list | None = None,
    fields: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
    order: str | None = None,
) -> str:
    """Search and read records from any Odoo model.

    Args:
        model: Odoo model technical name (e.g. "res.partner", "sale.order").
        domain: Search filter as a list of tuples, e.g. [["state","=","sale"]].
        fields: List of field names to return. None returns all fields.
        limit: Maximum number of records (default 20).
        offset: Number of records to skip for pagination.
        order: Sort string, e.g. "create_date desc".

    Returns:
        JSON string with matching records.
    """
    records = odoo.search_read(model, domain=domain, fields=fields,
                               limit=limit, offset=offset, order=order)
    return json.dumps({"model": model, "count": len(records),
                       "records": records}, default=str)


@mcp.tool()
def odoo_search_count(model: str, domain: list | None = None) -> str:
    """Count records matching a domain filter without fetching data.

    Use this before a large export to know how many records exist.

    Args:
        model: Odoo model technical name (e.g. "res.partner").
        domain: Search filter, e.g. [["active","=",true]]. None counts all.

    Returns:
        JSON string with the total count.
    """
    total = odoo.search_count(model, domain=domain)
    return json.dumps({"model": model, "count": total})


@mcp.tool()
def odoo_export(
    model: str,
    domain: list | None = None,
    fields: list[str] | None = None,
    limit: int = 500,
    offset: int = 0,
    order: str | None = None,
) -> str:
    """Export records in bulk for spreadsheet use. Higher default limit than search_read.

    Fetches up to 500 records per call (vs 20 for search_read). Use offset
    to paginate through large datasets. Pair with odoo_search_count to know
    the total.

    Args:
        model: Odoo model technical name.
        domain: Search filter. None returns all records.
        fields: Fields to export (column headers). None returns all fields.
        limit: Max records per call (default 500, max 2000).
        offset: Skip this many records (for pagination).
        order: Sort string, e.g. "id asc".

    Returns:
        JSON string with records, count returned, and offset for next page.
    """
    limit = min(limit, 2000)
    records = odoo.search_read(model, domain=domain, fields=fields,
                               limit=limit, offset=offset, order=order)
    return json.dumps({
        "model": model,
        "count": len(records),
        "offset": offset,
        "next_offset": offset + len(records) if len(records) == limit else None,
        "records": records,
    }, default=str)


@mcp.tool()
def odoo_create(model: str, values: dict) -> str:
    """Create a new record in an Odoo model.

    Args:
        model: Odoo model technical name (e.g. "res.partner").
        values: Field values for the new record, e.g. {"name": "Acme Corp"}.

    Returns:
        JSON string with the new record ID.
    """
    _check_writable()
    new_id = odoo.create(model, values)
    return json.dumps({"model": model, "operation": "create", "id": new_id})


@mcp.tool()
def odoo_update(model: str, ids: list[int], values: dict) -> str:
    """Update existing records in an Odoo model.

    Args:
        model: Odoo model technical name.
        ids: List of record IDs to update.
        values: Field values to write, e.g. {"phone": "+1-555-0100"}.

    Returns:
        JSON string confirming the update.
    """
    _check_writable()
    result = odoo.write(model, ids, values)
    return json.dumps({"model": model, "operation": "update",
                       "ids": ids, "success": result})


@mcp.tool()
def odoo_delete(model: str, ids: list[int]) -> str:
    """Delete records from an Odoo model.

    Args:
        model: Odoo model technical name.
        ids: List of record IDs to delete.

    Returns:
        JSON string confirming the deletion.
    """
    _check_writable()
    result = odoo.unlink(model, ids)
    return json.dumps({"model": model, "operation": "delete",
                       "ids": ids, "success": result})


@mcp.tool()
def odoo_execute(model: str, method: str, ids: list[int] | None = None) -> str:
    """Execute any model method (action) on Odoo records.

    Use this for workflow actions like action_confirm, action_post, etc.

    Args:
        model: Odoo model technical name (e.g. "account.move").
        method: Method name (e.g. "action_post", "action_confirm").
        ids: Record IDs to act on. Pass None or [] for methods that need no IDs.

    Returns:
        JSON string with the method result.
    """
    _check_writable()
    result = odoo.execute(model, method, ids or [])
    return json.dumps({"model": model, "method": method,
                       "ids": ids, "result": result}, default=str)


@mcp.tool()
def odoo_list_models(keyword: str = "") -> str:
    """List available Odoo models, optionally filtered by keyword.

    Args:
        keyword: Filter models whose technical name contains this string
                 (e.g. "sale", "stock", "account").

    Returns:
        JSON string with matching model names and descriptions.
    """
    domain: list = [["transient", "=", False]]
    if keyword:
        domain.append(["model", "ilike", keyword])

    models = odoo.search_read(
        "ir.model", domain=domain,
        fields=["model", "name"], limit=50, order="model",
    )
    return json.dumps({
        "count": len(models),
        "models": [{"model": m["model"], "name": m["name"]} for m in models],
    })


@mcp.tool()
def odoo_get_fields(model: str, attributes: list[str] | None = None) -> str:
    """Get field definitions for an Odoo model.

    Useful for discovering what fields a model has before reading/writing.

    Args:
        model: Odoo model technical name (e.g. "sale.order").
        attributes: Field attributes to return (e.g. ["string", "type", "required"]).
                    None returns all attributes.

    Returns:
        JSON string with field metadata.
    """
    attrs = attributes or ["string", "type", "required", "readonly", "help"]
    result = odoo.execute(model, "fields_get", attributes=attrs)
    return json.dumps({"model": model, "fields": result}, default=str)


@mcp.tool()
def odoo_doctor() -> str:
    """Run health diagnostics on the connected Odoo instance.

    Checks server version, installed modules, active users, cron jobs,
    and recent error logs.

    Returns:
        JSON string with diagnostic results.
    """
    checks = []

    # Server version
    checks.append({
        "check": "server_version",
        "status": "ok",
        "value": odoo.version,
    })

    # Installed modules
    try:
        modules = odoo.search_read(
            "ir.module.module",
            domain=[["state", "=", "installed"]],
            fields=["name", "shortdesc"], limit=200,
        )
        checks.append({
            "check": "installed_modules", "status": "ok",
            "value": len(modules),
            "modules": [m["name"] for m in modules],
        })
    except Exception as exc:
        checks.append({"check": "installed_modules", "status": "error", "value": str(exc)})

    # Active users
    try:
        users = odoo.search_read(
            "res.users", domain=[["active", "=", True]],
            fields=["login"], limit=500,
        )
        checks.append({"check": "active_users", "status": "ok", "value": len(users)})
    except Exception as exc:
        checks.append({"check": "active_users", "status": "error", "value": str(exc)})

    # Cron jobs
    try:
        crons = odoo.search_read(
            "ir.cron", domain=[["active", "=", True]],
            fields=["name", "interval_type", "interval_number", "nextcall"],
            limit=100,
        )
        checks.append({"check": "active_cron_jobs", "status": "ok", "value": len(crons)})
    except Exception as exc:
        checks.append({"check": "active_cron_jobs", "status": "error", "value": str(exc)})

    # Recent errors
    try:
        errors = odoo.search_read(
            "ir.logging",
            domain=[["level", "=", "ERROR"], ["type", "=", "server"]],
            fields=["name", "message", "create_date"],
            limit=5, order="create_date desc",
        )
        checks.append({
            "check": "recent_errors",
            "status": "warning" if errors else "ok",
            "value": len(errors), "errors": errors,
        })
    except Exception:
        checks.append({
            "check": "recent_errors", "status": "skipped",
            "value": "ir.logging not accessible",
        })

    ok_count = sum(1 for c in checks if c["status"] == "ok")
    return json.dumps({
        "instance": odoo.url, "database": odoo.database,
        "version": odoo.version,
        "summary": f"{ok_count}/{len(checks)} checks passed",
        "checks": checks,
    }, default=str)


# -- Model customization tools -----------------------------------------------

@mcp.tool()
def odoo_model_info(model: str) -> str:
    """Get comprehensive metadata about an Odoo model in one call.

    Returns the model's default sort order, record name field, field summary
    (grouped by type), custom fields (x_ prefix), views, window actions,
    and default values. Eliminates the need for multiple exploratory queries.

    Args:
        model: Odoo model technical name (e.g. "res.partner", "sale.order").

    Returns:
        JSON string with model metadata including fields, views, actions, and defaults.
    """
    result: dict[str, Any] = {"model": model}

    # Model-level metadata from ir.model. ir.model exposes 'order' as a
    # stored Char in both Odoo 18 and 19 (mirrors _order). However 'rec_name'
    # is NOT a stored field — _rec_name is a class attr. Infer it from the
    # fields list below.
    try:
        ir_models = odoo.search_read(
            "ir.model",
            domain=[["model", "=", model]],
            fields=["name", "model", "order", "state", "transient"],
            limit=1,
        )
        if not ir_models:
            return json.dumps({"error": f"Model '{model}' not found. Check odoo_list_models."})
        ir_model = ir_models[0]
        result["name"] = ir_model.get("name", "")
        result["default_order"] = ir_model.get("order", "id")
        result["state"] = ir_model.get("state", "")
        result["transient"] = ir_model.get("transient", False)
        model_id = ir_model["id"]
    except Exception as exc:
        return json.dumps({"error": f"Failed to read ir.model: {exc}"})

    # Field summary from ir.model.fields
    try:
        fields = odoo.search_read(
            "ir.model.fields",
            domain=[["model_id", "=", model_id]],
            fields=["name", "field_description", "ttype", "required",
                    "readonly", "store", "state", "relation",
                    "selection_ids", "tracking"],
            limit=500,
        )
        result["field_count"] = len(fields)

        # Infer rec_name (Odoo's own fallback: use 'name' if it exists)
        field_names = {f["name"] for f in fields}
        result["rec_name"] = "name" if "name" in field_names else ("x_name" if "x_name" in field_names else "id")

        # Group by type
        by_type: dict[str, int] = {}
        for f in fields:
            t = f.get("ttype", "?")
            by_type[t] = by_type.get(t, 0) + 1
        result["fields_by_type"] = by_type

        # Custom fields (user-created)
        custom = [
            {"name": f["name"], "type": f["ttype"], "label": f.get("field_description", "")}
            for f in fields if f.get("state") == "manual" or f["name"].startswith("x_")
        ]
        result["custom_fields"] = custom

        # Relational fields
        relations = [
            {"name": f["name"], "type": f["ttype"],
             "target": f.get("relation", ""), "label": f.get("field_description", "")}
            for f in fields if f.get("ttype") in ("many2one", "one2many", "many2many")
        ]
        result["relational_fields"] = relations

        # Required fields
        required = [
            {"name": f["name"], "type": f["ttype"], "label": f.get("field_description", "")}
            for f in fields if f.get("required")
        ]
        result["required_fields"] = required

    except Exception as exc:
        result["fields_error"] = str(exc)

    # Views
    try:
        views = odoo.search_read(
            "ir.ui.view",
            domain=[["model", "=", model], ["inherit_id", "=", False]],
            fields=["name", "type", "priority", "arch_db"],
            limit=20, order="type, priority",
        )
        result["views"] = [
            {"id": v["id"], "name": v.get("name", ""), "type": v.get("type", ""),
             "priority": v.get("priority", 16)}
            for v in views
        ]
    except Exception as exc:
        result["views_error"] = str(exc)

    # Window actions
    try:
        actions = odoo.search_read(
            "ir.actions.act_window",
            domain=[["res_model", "=", model]],
            fields=["name", "domain", "context", "view_mode", "limit"],
            limit=20,
        )
        result["actions"] = [
            {"id": a["id"], "name": a.get("name", ""),
             "domain": a.get("domain", ""), "context": a.get("context", ""),
             "view_mode": a.get("view_mode", ""), "limit": a.get("limit", 80)}
            for a in actions
        ]
    except Exception as exc:
        result["actions_error"] = str(exc)

    # Default values from ir.default
    try:
        field_ids = [f["id"] for f in fields] if fields else []
        if field_ids:
            defaults = odoo.search_read(
                "ir.default",
                domain=[["field_id", "in", field_ids]],
                fields=["field_id", "json_value", "user_id", "company_id"],
                limit=50,
            )
            result["defaults"] = [
                {"field": d.get("field_id", [None, ""])[1] if isinstance(d.get("field_id"), list) else d.get("field_id", ""),
                 "value": d.get("json_value", ""),
                 "user_id": d.get("user_id", False),
                 "company_id": d.get("company_id", False)}
                for d in defaults
            ]
        else:
            result["defaults"] = []
    except Exception as exc:
        result["defaults_error"] = str(exc)

    return json.dumps(result, default=str)


@mcp.tool()
def odoo_set_default(
    model: str,
    field_name: str,
    value: Any,
    user_id: int | None = None,
    company_id: int | None = None,
) -> str:
    """Set, update, or clear a field's default value.

    Manages ir.default records with proper JSON encoding. Set value to null
    to remove the default. Omit user_id for a global default.

    Args:
        model: Odoo model technical name (e.g. "product.template").
        field_name: Field name (e.g. "invoice_policy").
        value: Default value to set. Use the raw value — it will be JSON-encoded
               automatically. Pass null to remove the default.
        user_id: User ID for user-specific default. None or False = global default.
        company_id: Company ID for company-specific default. None or False = all companies.

    Returns:
        JSON string confirming the operation.
    """
    _check_writable()

    # Find the field_id
    field_records = odoo.search_read(
        "ir.model.fields",
        domain=[["model", "=", model], ["name", "=", field_name]],
        fields=["id", "name", "ttype", "field_description"],
        limit=1,
    )
    if not field_records:
        return json.dumps({
            "error": f"Field '{field_name}' not found on model '{model}'. "
                     f"Use odoo_get_fields('{model}') to see available fields."
        })

    field_id = field_records[0]["id"]
    field_type = field_records[0].get("ttype", "")
    field_label = field_records[0].get("field_description", field_name)

    # Build domain to find existing default
    search_domain: list = [["field_id", "=", field_id]]
    if user_id:
        search_domain.append(["user_id", "=", user_id])
    else:
        search_domain.append(["user_id", "=", False])
    if company_id:
        search_domain.append(["company_id", "=", company_id])
    else:
        search_domain.append(["company_id", "=", False])

    existing = odoo.search_read(
        "ir.default", domain=search_domain,
        fields=["id", "json_value"], limit=1,
    )

    # Remove default
    if value is None:
        if existing:
            odoo.unlink("ir.default", [existing[0]["id"]])
            return json.dumps({
                "model": model, "field": field_name, "operation": "removed",
                "previous_value": existing[0].get("json_value"),
            })
        return json.dumps({
            "model": model, "field": field_name, "operation": "no_default_found",
        })

    # JSON-encode the value
    json_value = json.dumps(value)

    if existing:
        # Update existing default
        old_value = existing[0].get("json_value")
        odoo.write("ir.default", [existing[0]["id"]], {"json_value": json_value})
        return json.dumps({
            "model": model, "field": field_name, "field_label": field_label,
            "field_type": field_type, "operation": "updated",
            "previous_value": old_value, "new_value": json_value,
            "user_id": user_id or False, "company_id": company_id or False,
        })
    else:
        # Create new default
        vals: dict[str, Any] = {"field_id": field_id, "json_value": json_value}
        if user_id:
            vals["user_id"] = user_id
        if company_id:
            vals["company_id"] = company_id
        new_id = odoo.create("ir.default", vals)
        return json.dumps({
            "model": model, "field": field_name, "field_label": field_label,
            "field_type": field_type, "operation": "created",
            "new_value": json_value, "id": new_id,
            "user_id": user_id or False, "company_id": company_id or False,
        })


@mcp.tool()
def odoo_get_view(
    model: str,
    view_type: str = "form",
) -> str:
    """Get the fully rendered (merged) view for a model.

    Returns the combined XML after all view inheritance is applied. This is
    what the user actually sees — not the raw fragments in ir.ui.view.

    Args:
        model: Odoo model technical name (e.g. "sale.order").
        view_type: View type — "form", "tree", "search", "kanban", "pivot",
                   "graph", "calendar". Defaults to "form".

    Returns:
        JSON string with the rendered view XML, view ID, and field names used.
    """
    try:
        view_data = odoo.execute(
            model, "get_views",
            [[False, view_type]],
        )
    except Exception:
        # Fallback for older Odoo versions
        try:
            view_data = odoo.execute(
                model, "fields_view_get",
                view_type=view_type,
            )
        except Exception as exc:
            return json.dumps({"error": f"Failed to get {view_type} view for {model}: {exc}"})

    result: dict[str, Any] = {"model": model, "view_type": view_type}

    # get_views returns a dict keyed by view type
    if isinstance(view_data, dict) and "views" in view_data:
        vdata = view_data["views"].get(view_type, {})
        result["view_id"] = vdata.get("id")
        result["arch"] = vdata.get("arch", "")
        # Extract field names from the view's fields dict
        fields_in_view = list(vdata.get("fields", {}).keys()) if "fields" in vdata else []
        result["fields_in_view"] = fields_in_view
    elif isinstance(view_data, dict):
        # fields_view_get format
        result["view_id"] = view_data.get("view_id")
        result["arch"] = view_data.get("arch", "")
        fields_in_view = list(view_data.get("fields", {}).keys()) if "fields" in view_data else []
        result["fields_in_view"] = fields_in_view
    else:
        result["raw"] = view_data

    # Truncate arch if very large
    arch = result.get("arch", "")
    if isinstance(arch, str) and len(arch) > 15000:
        result["arch"] = arch[:15000] + f"\n<!-- ... truncated ({len(arch)} chars total) -->"
        result["truncated"] = True

    return json.dumps(result, default=str)


@mcp.tool()
def odoo_modify_action(
    action_id: int | None = None,
    model: str | None = None,
    domain: str | None = None,
    context: str | None = None,
    order: str | None = None,
    limit: int | None = None,
    view_mode: str | None = None,
) -> str:
    """Modify a window action's properties to change list/form behavior.

    Window actions (ir.actions.act_window) control how a model appears in the
    UI — default filters, sort order, grouping, view modes, and record limits.

    Provide action_id to modify a specific action, or model to find and list
    actions for that model. Only provided fields are updated; others are unchanged.

    Args:
        action_id: ID of the ir.actions.act_window to modify. If omitted, uses
                   model to find actions.
        model: Model name to find actions for (used when action_id is not provided).
        domain: New domain filter string, e.g. "[['state','=','sale']]".
        context: New context string, e.g. "{'default_type': 'out_invoice',
                 'search_default_posted': 1, 'group_by': 'partner_id'}".
        order: Default sort order, e.g. "date_order desc, id desc". This sets
               the action's context key 'default_order' or modifies the associated
               tree view's default_order attribute.
        limit: Default number of records per page (e.g. 40, 80, 200).
        view_mode: Comma-separated view modes, e.g. "tree,form,kanban".

    Returns:
        JSON string with the action details before and after modification.
    """
    # Find the action
    if action_id:
        actions = odoo.search_read(
            "ir.actions.act_window",
            domain=[["id", "=", action_id]],
            fields=["name", "res_model", "domain", "context", "view_mode", "limit"],
            limit=1,
        )
    elif model:
        actions = odoo.search_read(
            "ir.actions.act_window",
            domain=[["res_model", "=", model]],
            fields=["name", "res_model", "domain", "context", "view_mode", "limit"],
            limit=10, order="id",
        )
    else:
        return json.dumps({"error": "Provide either action_id or model."})

    if not actions:
        target = f"action_id={action_id}" if action_id else f"model={model}"
        return json.dumps({"error": f"No window actions found for {target}."})

    # If no action_id and no modifications, just list actions
    no_changes = all(v is None for v in [domain, context, order, limit, view_mode])
    if not action_id and no_changes:
        return json.dumps({
            "model": model,
            "actions": [
                {"id": a["id"], "name": a.get("name", ""),
                 "domain": a.get("domain", ""), "context": a.get("context", ""),
                 "view_mode": a.get("view_mode", ""), "limit": a.get("limit", 80)}
                for a in actions
            ],
        })

    # If model provided without action_id, use the first action
    action = actions[0]
    aid = action["id"]

    if no_changes:
        return json.dumps({"action": action})

    _check_writable()

    # Build update values
    before = {k: action.get(k) for k in ["domain", "context", "view_mode", "limit"]}
    update_vals: dict[str, Any] = {}

    if domain is not None:
        update_vals["domain"] = domain
    if context is not None:
        update_vals["context"] = context
    if view_mode is not None:
        update_vals["view_mode"] = view_mode
    if limit is not None:
        update_vals["limit"] = limit

    # Handle order by merging default_order into the context. Use the
    # user-supplied context if provided in this same call, otherwise the
    # current DB value. Parse with ast.literal_eval — eval() on DB strings
    # would be remote code execution.
    if order is not None:
        ctx_source = update_vals.get("context", action.get("context", "")) or ""
        try:
            ctx_dict = ast.literal_eval(ctx_source) if ctx_source else {}
            if not isinstance(ctx_dict, dict):
                ctx_dict = {}
        except (ValueError, SyntaxError):
            ctx_dict = {}
        ctx_dict["default_order"] = order
        update_vals["context"] = repr(ctx_dict)

    odoo.write("ir.actions.act_window", [aid], update_vals)

    # Read back
    updated = odoo.search_read(
        "ir.actions.act_window",
        domain=[["id", "=", aid]],
        fields=["name", "res_model", "domain", "context", "view_mode", "limit"],
        limit=1,
    )
    after = {k: updated[0].get(k) for k in ["domain", "context", "view_mode", "limit"]} if updated else {}

    return json.dumps({
        "action_id": aid, "name": action.get("name", ""),
        "model": action.get("res_model", ""),
        "operation": "updated",
        "before": before, "after": after,
    }, default=str)


@mcp.tool()
def odoo_connection_info() -> str:
    """Show the current Odoo connection details.

    Returns:
        JSON string with URL, database, user, version, and uid.
    """
    return json.dumps({
        "url": odoo.url,
        "database": odoo.database,
        "username": odoo.username,
        "version": odoo.version,
        "uid": odoo.uid,
    })


# -- Resources ---------------------------------------------------------------

@mcp.resource("odoo://connection")
def connection_info() -> str:
    """Current Odoo connection metadata."""
    return json.dumps({
        "url": odoo.url, "database": odoo.database,
        "username": odoo.username, "version": odoo.version, "uid": odoo.uid,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global odoo
    odoo = _connect_from_env()
    mode = "READ-ONLY mode — write operations are disabled" if READONLY else "read-write mode"
    mcp.instructions = (
        f"Odoo ERP tools. You are connected to "
        f"{odoo.url} (database: {odoo.database}, Odoo {odoo.version}). "
        f"Running in {mode}."
    )
    mcp.run()


if __name__ == "__main__":
    main()
