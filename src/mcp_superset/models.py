"""Pydantic models for MCP tool parameters and responses."""

from pydantic import BaseModel, Field

# === Common models for filtering and pagination ===


class ListParams(BaseModel):
    """Pagination and filtering parameters for list endpoints."""

    page: int = Field(default=0, description="Page number (starting from 0)")
    page_size: int = Field(default=25, description="Page size (max 100)")
    q: str | None = Field(default=None, description="JSON filter in Superset RISON format")


# === Dashboard ===


class DashboardCreate(BaseModel):
    """Parameters for creating a new dashboard."""

    dashboard_title: str = Field(description="Dashboard title")
    slug: str | None = Field(default=None, description="URL slug")
    published: bool = Field(default=False, description="Whether the dashboard is published")
    json_metadata: str | None = Field(default=None, description="JSON metadata")
    css: str | None = Field(default=None, description="Custom CSS")
    position_json: str | None = Field(default=None, description="JSON widget positioning layout")


class DashboardUpdate(BaseModel):
    """Parameters for updating an existing dashboard."""

    dashboard_title: str | None = Field(default=None, description="Dashboard title")
    slug: str | None = Field(default=None, description="URL slug")
    published: bool | None = Field(default=None, description="Whether the dashboard is published")
    json_metadata: str | None = Field(default=None, description="JSON metadata")
    css: str | None = Field(default=None, description="Custom CSS")


# === Chart ===


class ChartCreate(BaseModel):
    """Parameters for creating a new chart."""

    slice_name: str = Field(description="Chart name")
    viz_type: str = Field(description="Visualization type (table, bar, line, pie, etc.)")
    datasource_id: int = Field(description="Dataset ID")
    datasource_type: str = Field(default="table", description="Datasource type")
    params: str | None = Field(default=None, description="JSON visualization parameters")
    query_context: str | None = Field(default=None, description="JSON query context")
    dashboards: list[int] | None = Field(default=None, description="Dashboard IDs to associate with")


class ChartUpdate(BaseModel):
    """Parameters for updating an existing chart."""

    slice_name: str | None = Field(default=None, description="Chart name")
    viz_type: str | None = Field(default=None, description="Visualization type")
    params: str | None = Field(default=None, description="JSON visualization parameters")
    query_context: str | None = Field(default=None, description="JSON query context")
    dashboards: list[int] | None = Field(default=None, description="Dashboard IDs to associate with")


# === Database ===


class DatabaseCreate(BaseModel):
    """Parameters for creating a new database connection."""

    database_name: str = Field(description="Connection name")
    sqlalchemy_uri: str = Field(description="SQLAlchemy connection URI")
    expose_in_sqllab: bool = Field(default=True, description="Whether to expose in SQL Lab")
    allow_ctas: bool = Field(default=False, description="Allow CREATE TABLE AS")
    allow_cvas: bool = Field(default=False, description="Allow CREATE VIEW AS")
    allow_dml: bool = Field(default=False, description="Allow DML operations")
    allow_run_async: bool = Field(default=False, description="Allow asynchronous queries")
    extra: str | None = Field(default=None, description="JSON extra settings")


class DatabaseUpdate(BaseModel):
    """Parameters for updating an existing database connection."""

    database_name: str | None = Field(default=None, description="Connection name")
    sqlalchemy_uri: str | None = Field(default=None, description="SQLAlchemy URI")
    expose_in_sqllab: bool | None = Field(default=None, description="Whether to expose in SQL Lab")
    allow_ctas: bool | None = Field(default=None, description="Allow CREATE TABLE AS")
    allow_cvas: bool | None = Field(default=None, description="Allow CREATE VIEW AS")
    allow_dml: bool | None = Field(default=None, description="Allow DML operations")
    extra: str | None = Field(default=None, description="JSON extra settings")


# === Dataset ===


class DatasetCreate(BaseModel):
    """Parameters for creating a new dataset."""

    table_name: str = Field(description="Table or view name")
    database: int = Field(description="Database connection ID")
    schema_name: str | None = Field(default=None, description="Schema (if not default)")
    sql: str | None = Field(default=None, description="SQL query for virtual dataset")


class DatasetUpdate(BaseModel):
    """Parameters for updating an existing dataset."""

    table_name: str | None = Field(default=None, description="Table or view name")
    sql: str | None = Field(default=None, description="SQL query for virtual dataset")
    description: str | None = Field(default=None, description="Dataset description")
    columns: list[dict] | None = Field(default=None, description="Dataset columns")
    metrics: list[dict] | None = Field(default=None, description="Dataset metrics")


# === SQL Lab ===


class SQLQuery(BaseModel):
    """Parameters for executing a SQL query in SQL Lab."""

    database_id: int = Field(description="Database connection ID")
    sql: str = Field(description="SQL query")
    schema: str | None = Field(default=None, description="Schema for execution")
    catalog: str | None = Field(default=None, description="Catalog for execution")
    tab_name: str | None = Field(default=None, description="Tab name in SQL Lab")
    template_params: str | None = Field(default=None, description="JSON Jinja template parameters")


class SavedQueryCreate(BaseModel):
    """Parameters for creating a saved query."""

    label: str = Field(description="Saved query name")
    db_id: int = Field(description="Database connection ID")
    sql: str = Field(description="SQL query")
    schema: str | None = Field(default=None, description="Schema")
    description: str | None = Field(default=None, description="Description")


# === Security ===


class UserCreate(BaseModel):
    """Parameters for creating a new user."""

    first_name: str = Field(description="First name")
    last_name: str = Field(description="Last name")
    username: str = Field(description="Username")
    email: str = Field(description="Email")
    password: str = Field(description="Password")
    roles: list[int] | None = Field(default=None, description="Role IDs")
    active: bool = Field(default=True, description="Whether the user is active")


class UserUpdate(BaseModel):
    """Parameters for updating an existing user."""

    first_name: str | None = Field(default=None, description="First name")
    last_name: str | None = Field(default=None, description="Last name")
    email: str | None = Field(default=None, description="Email")
    roles: list[int] | None = Field(default=None, description="Role IDs")
    active: bool | None = Field(default=None, description="Whether the user is active")


class RoleCreate(BaseModel):
    """Parameters for creating a new role."""

    name: str = Field(description="Role name")


class RoleUpdate(BaseModel):
    """Parameters for updating an existing role."""

    name: str = Field(description="New role name")


class RLSRuleCreate(BaseModel):
    """Parameters for creating a Row Level Security rule."""

    name: str = Field(description="RLS rule name")
    filter_type: str = Field(default="Regular", description="Filter type: Regular or Base")
    clause: str = Field(description="SQL condition (WHERE clause)")
    tables: list[int] = Field(description="Dataset IDs to apply the rule to")
    roles: list[int] = Field(description="Role IDs to apply the rule to")
    group_key: str | None = Field(default=None, description="Group key")
    description: str | None = Field(default=None, description="Rule description")


class RLSRuleUpdate(BaseModel):
    """Parameters for updating an existing RLS rule."""

    name: str | None = Field(default=None, description="RLS rule name")
    filter_type: str | None = Field(default=None, description="Filter type")
    clause: str | None = Field(default=None, description="SQL condition")
    tables: list[int] | None = Field(default=None, description="Dataset IDs")
    roles: list[int] | None = Field(default=None, description="Role IDs")
    group_key: str | None = Field(default=None, description="Group key")
    description: str | None = Field(default=None, description="Description")


# === Reports ===


class ReportCreate(BaseModel):
    """Parameters for creating a report or alert schedule."""

    name: str = Field(description="Report/alert name")
    type: str = Field(default="Report", description="Type: Report or Alert")
    crontab: str = Field(description="Cron schedule (e.g. '0 9 * * *')")
    dashboard: int | None = Field(default=None, description="Dashboard ID")
    chart: int | None = Field(default=None, description="Chart ID")
    database: int | None = Field(default=None, description="Database ID (for Alert)")
    sql: str | None = Field(default=None, description="SQL query (for Alert)")
    recipients: list[dict] | None = Field(default=None, description="Recipients")
    active: bool = Field(default=True, description="Whether the report is active")


class ReportUpdate(BaseModel):
    """Parameters for updating an existing report or alert."""

    name: str | None = Field(default=None, description="Name")
    crontab: str | None = Field(default=None, description="Cron schedule")
    active: bool | None = Field(default=None, description="Whether the report is active")
    recipients: list[dict] | None = Field(default=None, description="Recipients")
