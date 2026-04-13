# Odoo MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that connects AI agents to Odoo ERP instances. Works with Claude Code, Cursor, Windsurf, and any MCP-compatible client.

Supports **Odoo 17-18** (JSON-RPC) and **Odoo 19+** (JSON-2 API) — auto-detects the best protocol.

![Odoo MCP Server Overview](odoo-mcp.gif)

## Quick Start

```bash
# Run directly (uv handles dependencies automatically)
ODOO_URL=https://my.odoo.com ODOO_DB=mydb ODOO_USER=admin ODOO_PASSWORD=secret \
  uv run odoo_mcp_server.py
```

No virtualenv or `pip install` needed — the script has [inline metadata](https://packaging.python.org/en/latest/specifications/inline-script-metadata/) that `uv` resolves automatically.

## Configure in Claude Code

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "odoo": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--python", "3.11", "--script", "/path/to/odoo_mcp_server.py"],
      "env": {
        "ODOO_URL": "https://your-instance.odoo.com",
        "ODOO_DB": "your-database",
        "ODOO_USER": "admin",
        "ODOO_PASSWORD": "your-password"
      }
    }
  }
}
```

Or for Odoo 19+ with API key auth:

```json
{
  "mcpServers": {
    "odoo": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--python", "3.11", "--script", "/path/to/odoo_mcp_server.py"],
      "env": {
        "ODOO_URL": "https://your-instance.odoo.com",
        "ODOO_DB": "your-database",
        "ODOO_USER": "admin",
        "ODOO_API_KEY": "your-api-key"
      }
    }
  }
}
```

## Configure in Cursor / Windsurf

Add to `~/.cursor/mcp.json` or equivalent:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "uv",
      "args": ["run", "--python", "3.11", "--script", "/path/to/odoo_mcp_server.py"],
      "env": {
        "ODOO_URL": "https://your-instance.odoo.com",
        "ODOO_DB": "your-database",
        "ODOO_USER": "admin",
        "ODOO_PASSWORD": "your-password"
      }
    }
  }
}
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ODOO_URL` | Yes | Odoo instance URL |
| `ODOO_DB` | Yes | Database name |
| `ODOO_USER` | Yes | Login username |
| `ODOO_PASSWORD` | One of these | Password (Odoo 17-18) |
| `ODOO_API_KEY` | required | API key (Odoo 19+, preferred) |
| `ODOO_READONLY` | No | Set to `true` to disable all write operations |

### Read-Only Mode

Set `ODOO_READONLY=true` to disable `create`, `update`, `delete`, and `execute` tools. Useful for safe browsing of production instances:

```json
{
  "mcpServers": {
    "odoo": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--python", "3.11", "--script", "/path/to/odoo_mcp_server.py"],
      "env": {
        "ODOO_URL": "https://production.odoo.com",
        "ODOO_DB": "prod",
        "ODOO_USER": "readonly-user",
        "ODOO_PASSWORD": "secret",
        "ODOO_READONLY": "true"
      }
    }
  }
}
```

## Available Tools

### Core CRUD

| Tool | Description |
|---|---|
| `odoo_search_read` | Query records with domain filters, field selection, pagination |
| `odoo_search_count` | Count matching records without fetching data |
| `odoo_export` | Bulk export up to 2000 records per call for spreadsheets |
| `odoo_create` | Create new records |
| `odoo_update` | Update existing records by ID |
| `odoo_delete` | Delete records by ID |
| `odoo_execute` | Run any model method (action_confirm, action_post, etc.) |

### Discovery

| Tool | Description |
|---|---|
| `odoo_list_models` | Discover available models with keyword filter |
| `odoo_get_fields` | Inspect field definitions for any model |
| `odoo_doctor` | Health diagnostics (version, modules, users, crons, errors) |
| `odoo_connection_info` | Show current connection details |

### Model Customization

| Tool | Description |
|---|---|
| `odoo_model_info` | Get comprehensive model metadata in one call — fields, views, actions, defaults, sort order |
| `odoo_set_default` | Set, update, or clear a field's default value (handles ir.default + JSON encoding) |
| `odoo_get_view` | Get the fully rendered (merged) form/tree/search view XML |
| `odoo_modify_action` | Change a window action's domain, context, sort order, limit, or view modes |

## Example Usage

Once configured, ask your AI agent:

- "List all sale orders from this month"
- "Show me the fields on res.partner"
- "Create a new contact named Acme Corp"
- "Run a health check on the Odoo instance"
- "Export all products to a spreadsheet"
- "Confirm sale order 42"

### Model Customization Examples

- "What's the default sort order for sale.order?"
- "Change the default invoice policy on products to 'delivery'"
- "Show me the form view for res.partner"
- "What window actions exist for account.move? Change the default sort to date desc"
- "List all custom fields on res.partner"
- "What fields are required on sale.order?"

## Model Customization Tools — Detailed Reference

### odoo_model_info

Returns everything about a model in one call, eliminating the need for multiple exploratory queries.

```
odoo_model_info(model="sale.order")
```

Returns:
- `default_order` — the model's `_order` attribute (e.g. `"date_order desc, id desc"`)
- `rec_name` — the field used for display name in dropdowns
- `field_count` — total number of fields
- `fields_by_type` — field count grouped by type (`many2one: 12, char: 8, ...`)
- `custom_fields` — user-created fields (x_ prefix or state=manual)
- `relational_fields` — all Many2one, One2many, Many2many with their targets
- `required_fields` — fields that must be filled
- `views` — base views (form, tree, search) with IDs and priorities
- `actions` — window actions with their domain, context, and view modes
- `defaults` — current ir.default values set for this model's fields

### odoo_set_default

Manages field defaults via ir.default with proper JSON encoding. The most common source of agent errors when done manually.

```
# Set global default
odoo_set_default(model="product.template", field_name="invoice_policy", value="delivery")

# Set user-specific default
odoo_set_default(model="sale.order", field_name="warehouse_id", value=2, user_id=5)

# Remove a default
odoo_set_default(model="product.template", field_name="invoice_policy", value=null)
```

The tool:
1. Finds the field_id in ir.model.fields (validates the field exists)
2. JSON-encodes the value automatically
3. Creates or updates the ir.default record
4. Returns before/after values for confirmation

### odoo_get_view

Returns the fully rendered view XML after all inheritance is applied — what the user actually sees, not the raw fragments stored in ir.ui.view.

```
odoo_get_view(model="sale.order", view_type="form")
odoo_get_view(model="res.partner", view_type="tree")
odoo_get_view(model="account.move", view_type="search")
```

Returns:
- `arch` — the complete merged XML
- `view_id` — the base view ID
- `fields_in_view` — list of field names present in the view

### odoo_modify_action

Changes how a model appears in the UI by modifying its window action (ir.actions.act_window).

```
# List actions for a model (read-only)
odoo_modify_action(model="sale.order")

# Change default sort order
odoo_modify_action(action_id=42, order="date_order desc")

# Change default filter and page size
odoo_modify_action(action_id=42, domain="[['state','=','sale']]", limit=200)

# Add default grouping via context
odoo_modify_action(action_id=42, context="{'group_by': 'partner_id'}")
```

The tool returns before/after values so you can verify what changed.

## How It Works

```
AI Agent (Claude, Cursor, etc.)
    ↕ MCP Protocol (stdio)
Odoo MCP Server
    ↕ JSON-RPC / JSON-2 API
Odoo Instance
```

The server authenticates once at startup and maintains a persistent connection. All tools use the same authenticated session.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or `pip install fastmcp httpx`

## License

MIT

<!-- mcp-name: io.github.oconsole/odoo-simple-mcp -->
