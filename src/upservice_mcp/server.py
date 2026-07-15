#!/usr/bin/env python3
"""
MCP Server for the Upservice Public API (https://public.upservice.io).

Upservice is a project/task management and CRM platform. This server wraps the
public REST API (OpenAPI spec at https://public.upservice.io/openapi.json) and
exposes tools for working with employees, projects, sprints, tasks, tags,
directories/directory records, and channel messages.

Authentication:
    Upservice uses a simple API-key scheme. The key is sent as the raw value of
    the `Authorization` header (NOT prefixed with "Bearer "), e.g.:

        Authorization: UPS-XXXX-XXXX-XXXX-XXXX

    Set the key via the UPSERVICE_API_KEY environment variable. You can obtain
    a personal API key from your Upservice account settings.

Notes on coverage:
    This server aims for broad coverage of the documented endpoints. For
    request bodies with many optional/advanced fields, each tool exposes the
    core, commonly-used fields explicitly (validated via Pydantic) plus an
    optional `extra_fields` dict that is merged into the JSON body verbatim,
    so advanced/uncommon fields documented in the Upservice API can still be
    supplied without waiting for this server to be updated.
"""

import asyncio
import json
import os
from enum import Enum
from typing import Any, Dict, List, Optional, Union

import httpx
from pydantic import BaseModel, ConfigDict, Field
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE_URL = os.environ.get("UPSERVICE_API_BASE_URL", "https://public.upservice.io").rstrip("/")
API_KEY = os.environ.get("UPSERVICE_API_KEY", "")
MAX_RETRIES = 3
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

mcp = FastMCP("upservice_mcp")


# ---------------------------------------------------------------------------
# Shared enums (mirrors the Upservice OpenAPI schema)
# ---------------------------------------------------------------------------

class TaskKind(str, Enum):
    TASK = "task"
    MEETING = "meeting"
    AGREEMENT = "agreement"
    ACQUAINTANCE = "acquaintance"
    AGREEMENT_TASK = "agreement_task"
    TICKET = "ticket"


class TaskStatus(str, Enum):
    TODO = "todo"
    NOTASSIGNED = "notassigned"
    BACKLOG = "backlog"
    PROGRESS = "progress"
    REVIEW = "review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    DELETED = "deleted"
    PENDING = "pending"
    OUTDATED = "outdated"
    AI = "ai"
    ONHOLD = "onhold"


class AgreementAction(str, Enum):
    PROGRESS = "progress"
    APPROVED = "approved"
    REJECTED = "rejected"


class TagType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    ADDRESS = "address"
    DATE = "date"
    RICHARD = "richard"


class TagEntityType(str, Enum):
    TASK = "task"
    CHANNEL_CHAT = "channel_chat"
    ASSET = "asset"
    CONTACT = "contact"
    ATTACHMENT = "attachment"


class TagsCondition(str, Enum):
    CONTAINS_ANY = "contains_any"
    CONTAINS_ALL = "contains_all"
    NOT_CONTAIN_ALL = "not_contain_all"
    UNTAGGED = "untagged"
    ANY = "any"


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    DELETED = "deleted"


class SprintStatus(str, Enum):
    ACTIVE = "active"
    PLANNED = "planned"
    COMPLETED = "completed"
    DELETED = "deleted"


class DirectoryRelationType(str, Enum):
    ORDER = "order"
    ORDER_STATUS = "order_status"
    CONTACT = "contact"
    PROJECT = "project"
    TASK = "task"
    CHANNEL_CHAT = "channel_chat"
    ASSET = "asset"
    REQUEST = "request"
    TICKET = "ticket"


# ---------------------------------------------------------------------------
# Shared HTTP client / error handling
# ---------------------------------------------------------------------------

async def _request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Any:
    """Reusable async request function used by every tool in this server."""
    if not API_KEY:
        raise RuntimeError(
            "UPSERVICE_API_KEY is not set. Configure it as an environment variable "
            "before starting this server (see the API Key section in Upservice "
            "account settings)."
        )

    # Drop None values so we don't send empty query params / body fields.
    clean_params = _strip_none(params) if params else None
    clean_body = _strip_none(json_body) if json_body else None

    headers = {
        "Authorization": API_KEY,
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=60.0, follow_redirects=True) as client:
        for attempt in range(MAX_RETRIES + 1):
            response = await client.request(
                method,
                path,
                params=clean_params,
                json=clean_body,
                headers=headers,
            )
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else (2 ** attempt)
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw_response": response.text}


async def _upload_file(path: str, file_path: str) -> Any:
    """Upload a local file to Upservice via multipart/form-data (used by the /v1/files/ endpoints)."""
    if not API_KEY:
        raise RuntimeError(
            "UPSERVICE_API_KEY is not set. Configure it as an environment variable "
            "before starting this server (see the API Key section in Upservice "
            "account settings)."
        )
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"No such file: {file_path}")

    headers = {"Authorization": API_KEY, "Accept": "application/json"}
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=60.0, follow_redirects=True) as client:
            response = await client.post(path, files={"file": (filename, f)}, headers=headers)
            response.raise_for_status()
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw_response": response.text}


def _strip_none(obj: Any) -> Any:
    """Recursively remove keys whose value is None from dicts (leaves lists/values alone)."""
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    return obj


MENTION_HINT = (
    "To mention an employee (so they get notified), use the format @[First Last](employee_id) "
    "with the numeric employee_id from upservice_list_employees — plain @First Last does NOT "
    "create a mention or send a notification. Prefer the `mentions` field over hand-writing this "
    "syntax: put a `{{employee_id}}` placeholder where the mention should appear and it will be "
    "substituted automatically."
)


def _apply_mentions(text: Optional[str], mentions: Optional[List["MentionInput"]]) -> Optional[str]:
    """Replace `{{employee_id}}` placeholders in text with Upservice's @[Name](employee_id) mention syntax."""
    if text is None or not mentions:
        return text
    for m in mentions:
        text = text.replace(f"{{{{{m.employee_id}}}}}", f"@[{m.display_name}]({m.employee_id})")
    return text


def _handle_api_error(e: Exception) -> str:
    """Consistent, actionable error formatting across all tools."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        try:
            detail = e.response.json()
        except ValueError:
            detail = e.response.text
        if status == 401:
            return (
                "Error: Authentication failed (401). Check that UPSERVICE_API_KEY is set "
                "correctly and has not expired. Detail: " + str(detail)
            )
        if status == 403:
            return f"Error: Permission denied (403). Your API key may lack access to this resource. Detail: {detail}"
        if status == 404:
            return f"Error: Resource not found (404). Double-check the ID you provided. Detail: {detail}"
        if status == 422:
            return f"Error: Validation error (422) - the request payload was rejected. Detail: {detail}"
        if status == 429:
            return "Error: Rate limit exceeded (429). Wait before retrying."
        return f"Error: API request failed with status {status}. Detail: {detail}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request to Upservice timed out. Please try again."
    if isinstance(e, RuntimeError):
        return f"Error: {e}"
    return f"Error: Unexpected error occurred: {type(e).__name__}: {e}"


def _ok(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Shared pagination input
# ---------------------------------------------------------------------------

class PaginationInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    limit: Optional[int] = Field(default=25, description="Page size (max 100)", ge=1, le=100)
    offset: Optional[int] = Field(default=0, description="Number of items to skip for pagination", ge=0)


class MentionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: int = Field(..., description="Employee ID to mention (from upservice_list_employees)")
    display_name: str = Field(..., description="Display name to show for the mention, e.g. 'Ivan Ivanov'")


# ===========================================================================
# EMPLOYEES
# ===========================================================================

class ListEmployeesInput(PaginationInput):
    pass


@mcp.tool(
    name="upservice_list_employees",
    annotations={"title": "List Upservice Employees", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_list_employees(params: ListEmployeesInput) -> str:
    """List employees in the Upservice account.

    Args:
        params (ListEmployeesInput):
            - limit (Optional[int]): Page size, 1-100 (default 25)
            - offset (Optional[int]): Items to skip for pagination (default 0)

    Returns:
        str: JSON array/object of employees as returned by the Upservice API
        (fields typically include id, first_name, last_name, email, position, department).

    Error Handling:
        Returns "Error: ..." with an actionable message on failure (see error codes below).
    """
    try:
        data = await _request("GET", "/v1/employees", params=params.model_dump())
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


# ===========================================================================
# PROJECTS
# ===========================================================================

class ListProjectsInput(PaginationInput):
    status: Optional[List[ProjectStatus]] = Field(default=None, description="Filter by project status(es): active, completed, deleted")
    tags_condition: Optional[TagsCondition] = Field(default=None, description="How to combine tags_ids: contains_any, contains_all, not_contain_all, untagged, any")
    tags_ids: Optional[List[str]] = Field(default=None, description="Filter by one or more tag UUIDs")


@mcp.tool(
    name="upservice_list_projects",
    annotations={"title": "List Upservice Projects", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_list_projects(params: ListProjectsInput) -> str:
    """List projects in the Upservice account.

    Args:
        params (ListProjectsInput): limit (1-100, default 25), offset (default 0), status, tags_condition, tags_ids

    Returns:
        str: JSON list of projects (id, title, managers, members, completed, etc.)
    """
    try:
        p = params.model_dump(exclude={"status", "tags_condition"})
        if params.status:
            p["status"] = [s.value for s in params.status]
        if params.tags_condition:
            p["tags_condition"] = params.tags_condition.value
        data = await _request("GET", "/v1/projects", params=p)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class CreateProjectInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="Project title", max_length=512)
    managers: List[int] = Field(..., description="Employee IDs who will manage the project (at least 1)", min_length=1)
    members: List[int] = Field(..., description="Employee IDs who will be members of the project (at least 1)", min_length=1)
    extra_fields: Optional[Dict[str, Any]] = Field(
        default=None, description="Additional raw fields to merge into the request body, for advanced/uncommon options documented in the Upservice API."
    )


@mcp.tool(
    name="upservice_create_project",
    annotations={"title": "Create Upservice Project", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_create_project(params: CreateProjectInput) -> str:
    """Create a new project in Upservice.

    Args:
        params (CreateProjectInput):
            - title (str): Project title
            - managers (List[int]): Employee IDs to assign as project managers
            - members (List[int]): Employee IDs to assign as project members
            - extra_fields (Optional[dict]): Extra raw JSON fields to merge into the body

    Returns:
        str: JSON of the created project record.
    """
    try:
        body = {"title": params.title, "managers": params.managers, "members": params.members}
        if params.extra_fields:
            body.update(params.extra_fields)
        data = await _request("POST", "/v1/projects", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class ProjectIdInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(..., description="The Upservice project ID")


@mcp.tool(
    name="upservice_get_project",
    annotations={"title": "Get Upservice Project", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_project(params: ProjectIdInput) -> str:
    """Retrieve a single project by ID.

    Args:
        params (ProjectIdInput): project_id (int)

    Returns:
        str: JSON of the project record, or "Error: Resource not found (404)" if it doesn't exist.
    """
    try:
        data = await _request("GET", f"/v1/projects/{params.project_id}")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class UpdateProjectInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    project_id: int = Field(..., description="The Upservice project ID to update")
    title: Optional[str] = Field(default=None, description="New project title", max_length=512)
    extra_fields: Optional[Dict[str, Any]] = Field(default=None, description="Additional raw fields to merge into the request body")


@mcp.tool(
    name="upservice_update_project",
    annotations={"title": "Update Upservice Project", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_update_project(params: UpdateProjectInput) -> str:
    """Update an existing project's fields (e.g. title).

    Args:
        params (UpdateProjectInput): project_id (int), title (optional str), extra_fields (optional dict)

    Returns:
        str: JSON of the updated project record.
    """
    try:
        body: Dict[str, Any] = {}
        if params.title is not None:
            body["title"] = params.title
        if params.extra_fields:
            body.update(params.extra_fields)
        data = await _request("PUT", f"/v1/projects/{params.project_id}", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_delete_project",
    annotations={"title": "Delete Upservice Project", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_delete_project(params: ProjectIdInput) -> str:
    """Permanently delete a project. This is a destructive operation and cannot be undone.

    Args:
        params (ProjectIdInput): project_id (int)

    Returns:
        str: JSON confirmation, or "Error: ..." on failure.
    """
    try:
        data = await _request("DELETE", f"/v1/projects/{params.project_id}")
        return _ok(data) if data else "Project deleted successfully."
    except Exception as e:
        return _handle_api_error(e)


class SetProjectEmployeesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(..., description="The Upservice project ID")
    employees: List[int] = Field(..., description="Employee IDs (this REPLACES the current set; Upservice requires at least one)", min_length=1)


@mcp.tool(
    name="upservice_set_project_managers",
    annotations={"title": "Set Upservice Project Managers", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_set_project_managers(params: SetProjectEmployeesInput) -> str:
    """Replace the full set of managers for a project.

    Note: this REPLACES the existing manager list, it does not append to it.

    Args:
        params (SetProjectEmployeesInput): project_id (int), employees (List[int], the new full manager list)

    Returns:
        str: JSON confirmation/updated manager list.
    """
    try:
        data = await _request("POST", f"/v1/projects/{params.project_id}/managers", json_body={"employees": params.employees})
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_set_project_members",
    annotations={"title": "Set Upservice Project Members", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_set_project_members(params: SetProjectEmployeesInput) -> str:
    """Replace the full set of members/guests for a project.

    Note: this REPLACES the existing membership list, it does not append to it.

    Args:
        params (SetProjectEmployeesInput): project_id (int), employees (List[int], the new full member list)

    Returns:
        str: JSON confirmation/updated member list.
    """
    try:
        data = await _request("POST", f"/v1/projects/{params.project_id}/members", json_body={"employees": params.employees})
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_complete_project",
    annotations={"title": "Mark Upservice Project Completed", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_complete_project(params: ProjectIdInput) -> str:
    """Mark a project as completed.

    Args:
        params (ProjectIdInput): project_id (int)

    Returns:
        str: JSON of the updated project record.
    """
    try:
        data = await _request("PUT", f"/v1/projects/{params.project_id}/completed")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


# ===========================================================================
# SPRINTS
# ===========================================================================

class ListSprintsInput(PaginationInput):
    project: Optional[List[int]] = Field(default=None, description="Filter by project ID(s)")
    status: Optional[List[SprintStatus]] = Field(default=None, description="Filter by sprint status(es): active, planned, completed, deleted")
    is_lag: Optional[bool] = Field(default=None, description="True: only delayed sprints; False: on-time sprints")


@mcp.tool(
    name="upservice_list_sprints",
    annotations={"title": "List Upservice Sprints", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_list_sprints(params: ListSprintsInput) -> str:
    """List sprints, optionally filtered by project.

    Args:
        params (ListSprintsInput): limit, offset, project (optional list of IDs), status (optional list), is_lag (optional bool)

    Returns:
        str: JSON list of sprints.
    """
    try:
        p = params.model_dump(exclude={"status"})
        if params.status:
            p["status"] = [s.value for s in params.status]
        data = await _request("GET", "/v1/sprints", params=p)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class CreateSprintInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="Sprint title", max_length=1024)
    project: int = Field(..., description="Project ID this sprint belongs to")
    date_start: str = Field(..., description="Start date, format YYYY-MM-DD")
    date_end: str = Field(..., description="End date, format YYYY-MM-DD")
    extra_fields: Optional[Dict[str, Any]] = Field(default=None, description="Additional raw fields to merge into the request body")


@mcp.tool(
    name="upservice_create_sprint",
    annotations={"title": "Create Upservice Sprint", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_create_sprint(params: CreateSprintInput) -> str:
    """Create a new sprint under a project.

    Args:
        params (CreateSprintInput): title, project (int), date_start (YYYY-MM-DD), date_end (YYYY-MM-DD), extra_fields (optional dict)

    Returns:
        str: JSON of the created sprint.
    """
    try:
        body = {
            "title": params.title,
            "project": params.project,
            "date_start": params.date_start,
            "date_end": params.date_end,
        }
        if params.extra_fields:
            body.update(params.extra_fields)
        data = await _request("POST", "/v1/sprints", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class SprintIdInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sprint_id: int = Field(..., description="The Upservice sprint ID")


@mcp.tool(
    name="upservice_get_sprint",
    annotations={"title": "Get Upservice Sprint", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_sprint(params: SprintIdInput) -> str:
    """Retrieve a single sprint by ID.

    Args:
        params (SprintIdInput): sprint_id (int)

    Returns:
        str: JSON of the sprint record.
    """
    try:
        data = await _request("GET", f"/v1/sprints/{params.sprint_id}")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class UpdateSprintInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    sprint_id: int = Field(..., description="The Upservice sprint ID to update")
    title: Optional[str] = Field(default=None, description="New sprint title")
    date_start: Optional[str] = Field(default=None, description="New start date, format YYYY-MM-DD")
    date_end: Optional[str] = Field(default=None, description="New end date, format YYYY-MM-DD")
    extra_fields: Optional[Dict[str, Any]] = Field(default=None, description="Additional raw fields to merge into the request body")


@mcp.tool(
    name="upservice_update_sprint",
    annotations={"title": "Update Upservice Sprint", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_update_sprint(params: UpdateSprintInput) -> str:
    """Update a sprint's title or dates.

    Args:
        params (UpdateSprintInput): sprint_id (int), title/date_start/date_end (all optional), extra_fields (optional dict)

    Returns:
        str: JSON of the updated sprint.
    """
    try:
        body: Dict[str, Any] = {"title": params.title, "date_start": params.date_start, "date_end": params.date_end}
        if params.extra_fields:
            body.update(params.extra_fields)
        data = await _request("PUT", f"/v1/sprints/{params.sprint_id}", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_delete_sprint",
    annotations={"title": "Delete Upservice Sprint", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_delete_sprint(params: SprintIdInput) -> str:
    """Permanently delete a sprint. This is a destructive operation.

    Args:
        params (SprintIdInput): sprint_id (int)

    Returns:
        str: JSON confirmation, or "Error: ..." on failure.
    """
    try:
        data = await _request("DELETE", f"/v1/sprints/{params.sprint_id}")
        return _ok(data) if data else "Sprint deleted successfully."
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_complete_sprint",
    annotations={"title": "Complete Upservice Sprint", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_complete_sprint(params: SprintIdInput) -> str:
    """Mark a sprint as completed.

    Args:
        params (SprintIdInput): sprint_id (int)

    Returns:
        str: JSON of the updated sprint.
    """
    try:
        data = await _request("POST", f"/v1/sprints/{params.sprint_id}/complete")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_activate_sprint",
    annotations={"title": "Activate Upservice Sprint", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_activate_sprint(params: SprintIdInput) -> str:
    """Activate a sprint (start it).

    Args:
        params (SprintIdInput): sprint_id (int)

    Returns:
        str: JSON of the updated sprint.
    """
    try:
        data = await _request("POST", f"/v1/sprints/{params.sprint_id}/activate")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class AddTasksToSprintInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sprint_id: int = Field(..., description="The Upservice sprint ID")
    tasks: List[int] = Field(..., description="Task IDs to add to this sprint (at least 1)", min_length=1)


@mcp.tool(
    name="upservice_add_tasks_to_sprint",
    annotations={"title": "Add Tasks To Upservice Sprint", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_add_tasks_to_sprint(params: AddTasksToSprintInput) -> str:
    """Add one or more existing tasks to a sprint.

    Args:
        params (AddTasksToSprintInput): sprint_id (int), tasks (List[int] of task IDs)

    Returns:
        str: JSON list of task IDs now in the sprint.
    """
    try:
        data = await _request("POST", f"/v1/sprints/{params.sprint_id}/add-tasks", json_body={"tasks": params.tasks})
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


# ===========================================================================
# TAGS
# ===========================================================================

class ListTagsInput(PaginationInput):
    query: Optional[str] = Field(default=None, description="Free-text search over tag names")


@mcp.tool(
    name="upservice_list_tags",
    annotations={"title": "List Upservice Tags", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_list_tags(params: ListTagsInput) -> str:
    """List tags defined in the Upservice account, optionally filtered by a search query.

    Args:
        params (ListTagsInput): limit, offset, query (optional str)

    Returns:
        str: JSON list of tags (id, name, color, type).
    """
    try:
        data = await _request("GET", "/v1/tags", params=params.model_dump())
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class CreateTagInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., description="Tag name")
    color: Optional[str] = Field(default=None, description="Tag color (implementation-specific string/hex)")
    type: Optional[TagType] = Field(default=TagType.TEXT, description="Tag value type. Defaults to 'text'.")


@mcp.tool(
    name="upservice_create_tag",
    annotations={"title": "Create Upservice Tag", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_create_tag(params: CreateTagInput) -> str:
    """Create a new tag definition.

    Args:
        params (CreateTagInput): name (str), color (optional str), type (optional: text|number|address|date|richard)

    Returns:
        str: JSON of the created tag, including its UUID.
    """
    try:
        body = {"name": params.name, "color": params.color, "type": params.type.value if params.type else None}
        data = await _request("POST", "/v1/tags", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class UpdateTagInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    tag_id: str = Field(..., description="The tag's UUID")
    name: Optional[str] = Field(default=None, description="New tag name")
    color: Optional[str] = Field(default=None, description="New tag color")


@mcp.tool(
    name="upservice_update_tag",
    annotations={"title": "Update Upservice Tag", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_update_tag(params: UpdateTagInput) -> str:
    """Update an existing tag's name or color.

    Args:
        params (UpdateTagInput): tag_id (UUID str), name (optional), color (optional)

    Returns:
        str: JSON of the updated tag.
    """
    try:
        body = {"name": params.name, "color": params.color}
        data = await _request("PUT", f"/v1/tags/{params.tag_id}", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_delete_tag",
    annotations={"title": "Delete Upservice Tag", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_delete_tag(params: UpdateTagInput) -> str:
    """Permanently delete a tag definition (removing it from all entities it was applied to).

    Args:
        params (UpdateTagInput): only tag_id is used (UUID str)

    Returns:
        str: JSON confirmation, or "Error: ..." on failure.
    """
    try:
        data = await _request("DELETE", f"/v1/tags/{params.tag_id}")
        return _ok(data) if data else "Tag deleted successfully."
    except Exception as e:
        return _handle_api_error(e)


class AssignTagInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag_id: str = Field(..., description="The tag's UUID")
    entity_id: Union[str, int] = Field(..., description="ID of the entity to tag (UUID string or integer ID depending on entity_type)")
    entity_type: TagEntityType = Field(..., description="Type of entity being tagged: task, channel_chat, asset, contact, or attachment")


@mcp.tool(
    name="upservice_assign_tag",
    annotations={"title": "Assign Upservice Tag", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_assign_tag(params: AssignTagInput) -> str:
    """Assign (attach) a tag to an entity such as a task, chat, asset, contact, or attachment.

    Args:
        params (AssignTagInput): tag_id (UUID), entity_id (UUID or int), entity_type (enum)

    Returns:
        str: JSON confirmation of the assignment.
    """
    try:
        body = {"tag_id": params.tag_id, "entity_id": params.entity_id, "entity_type": params.entity_type.value}
        data = await _request("POST", "/v1/tags/assign", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_unassign_tag",
    annotations={"title": "Unassign Upservice Tag", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_unassign_tag(params: AssignTagInput) -> str:
    """Remove (detach) a tag from an entity.

    Args:
        params (AssignTagInput): tag_id (UUID), entity_id (UUID or int), entity_type (enum)

    Returns:
        str: JSON confirmation, or "Error: ..." on failure.
    """
    try:
        data = await _request(
            "DELETE", f"/v1/tags/assign/{params.entity_type.value}/{params.entity_id}/{params.tag_id}"
        )
        return _ok(data) if data else "Tag unassigned successfully."
    except Exception as e:
        return _handle_api_error(e)


# ===========================================================================
# TASKS
# ===========================================================================

class ListTasksInput(PaginationInput):
    kind: Optional[TaskKind] = Field(default=None, description="Filter by task kind. If omitted, only task, meeting, agreement, acquaintance and agreement_task are returned.")
    project: Optional[List[int]] = Field(default=None, description="Filter by project ID(s)")
    author: Optional[int] = Field(default=None, description="Filter by author employee ID")
    responsible: Optional[int] = Field(default=None, description="Filter by responsible employee ID")
    created_at_gte: Optional[str] = Field(default=None, description="Created at (from), ISO 8601")
    created_at_lte: Optional[str] = Field(default=None, description="Created at (to), ISO 8601")
    completed_at_gte: Optional[str] = Field(default=None, description="Completed at (from), ISO 8601")
    completed_at_lte: Optional[str] = Field(default=None, description="Completed at (to), ISO 8601")
    date_start_gte: Optional[str] = Field(default=None, description="Start date (from), ISO 8601")
    date_start_lte: Optional[str] = Field(default=None, description="Start date (to), ISO 8601")
    date_end_gte: Optional[str] = Field(default=None, description="Due date (from), ISO 8601")
    date_end_lte: Optional[str] = Field(default=None, description="Due date (to), ISO 8601")
    tags_ids: Optional[List[str]] = Field(default=None, description="Filter by one or more tag UUIDs")
    tags_condition: Optional[TagsCondition] = Field(default=None, description="How to combine tags_ids: contains_any, contains_all, not_contain_all, untagged, any")


@mcp.tool(
    name="upservice_list_tasks",
    annotations={"title": "List Upservice Tasks", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_list_tasks(params: ListTasksInput) -> str:
    """List/search tasks with optional filters for kind, project, author, responsible, date ranges, and tags.

    CONFIRMED LIMITATION (verified against the live API, not just the OpenAPI spec): this
    endpoint has no status/status_in/is_completed/completed/query filter. Passing any of
    those as extra query params is silently ignored server-side (no error, no effect on
    results) — status filtering must be done client-side on the returned `status` field.
    There is no workaround on the Upservice API today; this has been reported to Upservice
    as a feature request. Until it lands, narrow results with `date_end_gte`/`date_end_lte`
    (due-date range) plus `project`/`author`/`responsible` BEFORE paginating and filtering
    by status client-side — e.g. for "open overdue tasks", pass
    date_end_lte=<now, ISO 8601> together with project/responsible to get a small candidate
    set, then drop any whose `status` is completed/cancelled/rejected/deleted. Do not call
    this with only `author` (or no filters) and try to page through everything — accounts
    can have 100k+ tasks and completed_at/date filters are the only server-side way to keep
    that bounded.

    Args:
        params (ListTasksInput): limit, offset, kind, project, author, responsible,
            created_at_gte/lte, completed_at_gte/lte, date_start_gte/lte, date_end_gte/lte,
            tags_ids, tags_condition (all optional except pagination defaults)

    Returns:
        str: JSON list of tasks matching the filters.
    """
    try:
        p = params.model_dump(exclude={"kind", "tags_condition"})
        if params.kind:
            p["kind"] = params.kind.value
        if params.tags_condition:
            p["tags_condition"] = params.tags_condition.value
        data = await _request("GET", "/v1/tasks", params=p)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class CreateTaskInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="Task title", min_length=2, max_length=512)
    kind: TaskKind = Field(default=TaskKind.TASK, description="Task type. Defaults to 'task'.")
    description: Optional[str] = Field(default=None, description=f"Task description. {MENTION_HINT}")
    project: Optional[int] = Field(default=None, description="Project ID")
    sprint: Optional[int] = Field(default=None, description="Sprint ID")
    responsible: Optional[int] = Field(default=None, description="Employee ID of the responsible person (mutually exclusive with responsible_departments)")
    responsible_departments: Optional[List[int]] = Field(default=None, description="Department IDs (only for kind=task/agreement_task; mutually exclusive with responsible)")
    estimation: Optional[int] = Field(default=None, description="Planned effort in minutes", ge=0)
    date_start: Optional[str] = Field(default=None, description="Start date, ISO 8601 (required for kind=meeting)")
    date_end: Optional[str] = Field(default=None, description="Due date, ISO 8601 (required for kind=task/meeting/agreement/agreement_task; optional for ticket)")
    file_list: Optional[List[Union[int, str]]] = Field(default=None, description="Attachment IDs (int) or uploaded file UUIDs (str) to attach")
    mentions: Optional[List[MentionInput]] = Field(default=None, description="Employees to mention in `description`. Put a `{{employee_id}}` placeholder in the description text for each mention; it will be substituted with the correct @[Name](id) syntax.")
    extra_fields: Optional[Dict[str, Any]] = Field(default=None, description="Additional raw fields (e.g. tags, co_responsibles, priority) to merge into the request body")


@mcp.tool(
    name="upservice_create_task",
    annotations={"title": "Create Upservice Task", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_create_task(params: CreateTaskInput) -> str:
    """Create a new task (or meeting/agreement/ticket, depending on `kind`).

    Args:
        params (CreateTaskInput): title, kind, description, project, sprint, responsible,
            responsible_departments, estimation (minutes), date_start, date_end, file_list, mentions, extra_fields

    Returns:
        str: JSON of the created task record, including its ID.

    Examples:
        - "Create a task 'Fix login bug' in project 42, due 2026-08-01" ->
          title="Fix login bug", project=42, date_end="2026-08-01T00:00:00Z"
        - "Create a task and mention Ivan (employee_id 115768) in the description" ->
          description="cc {{115768}}", mentions=[{"employee_id": 115768, "display_name": "Ivan Ivanov"}]
    """
    try:
        body = params.model_dump(exclude={"extra_fields", "mentions"})
        body["kind"] = params.kind.value
        body["description"] = _apply_mentions(params.description, params.mentions)
        if params.extra_fields:
            body.update(params.extra_fields)
        data = await _request("POST", "/v1/tasks", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class TaskIdInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: int = Field(..., description="The Upservice task ID")


@mcp.tool(
    name="upservice_get_task",
    annotations={"title": "Get Upservice Task", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_task(params: TaskIdInput) -> str:
    """Retrieve full details for a single task by ID.

    Args:
        params (TaskIdInput): task_id (int)

    Returns:
        str: JSON of the task record.
    """
    try:
        data = await _request("GET", f"/v1/tasks/{params.task_id}")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class UpdateTaskInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    task_id: int = Field(..., description="The Upservice task ID to update")
    title: Optional[str] = Field(default=None, description="New task title", min_length=2, max_length=512)
    description: Optional[str] = Field(default=None, description=f"New description. Omit to leave unchanged; pass an empty string to clear if supported. {MENTION_HINT}")
    date_start: Optional[str] = Field(default=None, description="New start date, ISO 8601")
    date_end: Optional[str] = Field(default=None, description="New due date, ISO 8601")
    responsible: Optional[int] = Field(default=None, description="New responsible employee ID")
    mentions: Optional[List[MentionInput]] = Field(default=None, description="Employees to mention in `description`. Put a `{{employee_id}}` placeholder in the description text for each mention; it will be substituted with the correct @[Name](id) syntax.")
    extra_fields: Optional[Dict[str, Any]] = Field(default=None, description="Additional raw fields to merge into the request body")


@mcp.tool(
    name="upservice_update_task",
    annotations={"title": "Update Upservice Task", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_update_task(params: UpdateTaskInput) -> str:
    """Partially update a task's fields (title, description, dates, responsible, etc).

    Only fields you provide are changed; omitted fields are left unchanged.

    Args:
        params (UpdateTaskInput): task_id (int) plus any fields to change, mentions (for description), and extra_fields for anything not explicitly modeled

    Returns:
        str: JSON of the updated task record.
    """
    try:
        body = params.model_dump(exclude={"task_id", "extra_fields", "mentions"})
        if params.description is not None:
            body["description"] = _apply_mentions(params.description, params.mentions)
        if params.extra_fields:
            body.update(params.extra_fields)
        data = await _request("PATCH", f"/v1/tasks/{params.task_id}", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_delete_task",
    annotations={"title": "Delete Upservice Task", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_delete_task(params: TaskIdInput) -> str:
    """Permanently delete a task. This is a destructive operation and cannot be undone.

    Args:
        params (TaskIdInput): task_id (int)

    Returns:
        str: JSON confirmation, or "Error: ..." on failure.
    """
    try:
        data = await _request("DELETE", f"/v1/tasks/{params.task_id}")
        return _ok(data) if data else "Task deleted successfully."
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_get_task_attachments",
    annotations={"title": "Get Upservice Task Attachments", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_task_attachments(params: TaskIdInput) -> str:
    """List file attachments on a task.

    Args:
        params (TaskIdInput): task_id (int)

    Returns:
        str: JSON list of attachments (id, filename, url, etc. - use upservice_get_file_url for a fresh download link).
    """
    try:
        data = await _request("GET", f"/v1/tasks/{params.task_id}/attachments")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class UpdateTaskStatusInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    task_id: int = Field(..., description="The Upservice task ID")
    status: TaskStatus = Field(..., description="New status: one of todo, backlog, notassigned, progress, review, completed, cancelled, rejected, deleted, pending, outdated, ai, onhold")
    reason: Optional[str] = Field(default=None, description="Reason/comment, typically required when cancelling/rejecting", min_length=4, max_length=2048)


@mcp.tool(
    name="upservice_update_task_status",
    annotations={"title": "Update Upservice Task Status", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_update_task_status(params: UpdateTaskStatusInput) -> str:
    """Change a task's status (e.g. mark as completed, cancelled, in progress).

    Args:
        params (UpdateTaskStatusInput): task_id (int), status (enum), reason (optional str, needed for cancel/reject)

    Returns:
        str: JSON of the updated task.
    """
    try:
        body = {"status": params.status.value, "reason": params.reason}
        data = await _request("PUT", f"/v1/tasks/{params.task_id}/status", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class UpdateTaskEstimationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: int = Field(..., description="The Upservice task ID")
    estimation: int = Field(..., description="Planned effort in minutes", ge=0)


@mcp.tool(
    name="upservice_update_task_estimation",
    annotations={"title": "Update Upservice Task Estimation", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_update_task_estimation(params: UpdateTaskEstimationInput) -> str:
    """Set the planned-effort estimate for a task, in minutes.

    Args:
        params (UpdateTaskEstimationInput): task_id (int), estimation (int, minutes)

    Returns:
        str: JSON of the updated task.
    """
    try:
        data = await _request("PUT", f"/v1/tasks/{params.task_id}/estimation", json_body={"estimation": params.estimation})
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class UpdateTaskWorklogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: int = Field(..., description="The Upservice task ID")
    value: int = Field(..., description="Actual effort logged, in minutes", ge=0)


@mcp.tool(
    name="upservice_update_task_worklog",
    annotations={"title": "Log Upservice Task Worklog", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_update_task_worklog(params: UpdateTaskWorklogInput) -> str:
    """Log actual effort (worklog) spent on a task, in minutes.

    Args:
        params (UpdateTaskWorklogInput): task_id (int), value (int, minutes of actual effort)

    Returns:
        str: JSON of the updated task/worklog.
    """
    try:
        data = await _request("POST", f"/v1/tasks/{params.task_id}/worklog", json_body={"value": params.value})
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class AgreementActionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    task_id: int = Field(..., description="The Upservice task ID (should be an agreement/agreement_task)")
    action: AgreementAction = Field(..., description="One of: progress, approved, rejected")
    rejection_reason: Optional[str] = Field(default=None, description="Required when action='rejected'", min_length=4)
    date_end: Optional[str] = Field(default=None, description="Optional new deadline when action='progress'")


@mcp.tool(
    name="upservice_task_agreement_action",
    annotations={"title": "Perform Upservice Task Agreement Action", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_task_agreement_action(params: AgreementActionInput) -> str:
    """Advance an agreement/approval workflow step on a task: progress it, approve it, or reject it.

    Args:
        params (AgreementActionInput): task_id (int), action (progress|approved|rejected),
            rejection_reason (required if action='rejected'), date_end (optional, used with 'progress')

    Returns:
        str: JSON of the updated agreement state.
    """
    try:
        body = {
            "action": params.action.value,
            "rejection_reason": params.rejection_reason,
            "date_end": params.date_end,
        }
        data = await _request("PUT", f"/v1/tasks/{params.task_id}/agreement-action", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_get_agreement_steps",
    annotations={"title": "Get Upservice Task Agreement Steps", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_agreement_steps(params: TaskIdInput) -> str:
    """List the approval/agreement steps and their statuses for a task.

    Args:
        params (TaskIdInput): task_id (int)

    Returns:
        str: JSON list of agreement steps.
    """
    try:
        data = await _request("GET", f"/v1/tasks/{params.task_id}/agreement-steps")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_get_task_co_responsibles",
    annotations={"title": "Get Upservice Task Co-Responsibles", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_task_co_responsibles(params: TaskIdInput) -> str:
    """List the co-responsible employees assigned to a task.

    Args:
        params (TaskIdInput): task_id (int)

    Returns:
        str: JSON list of co-responsible employees.
    """
    try:
        data = await _request("GET", f"/v1/tasks/{params.task_id}/co-responsibles")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_get_agreement_sheet",
    annotations={"title": "Get Upservice Task Agreement Sheet", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_agreement_sheet(params: TaskIdInput) -> str:
    """Get the agreement sheet (approval status and attachments) for a task.

    Args:
        params (TaskIdInput): task_id (int)

    Returns:
        str: JSON with status and attachments for the agreement.
    """
    try:
        data = await _request("GET", f"/v1/tasks/{params.task_id}/agreement-sheet")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_get_acquaintance_sheet",
    annotations={"title": "Get Upservice Task Acquaintance Sheet", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_acquaintance_sheet(params: TaskIdInput) -> str:
    """Get the acquaintance sheet (who has read/acknowledged) for a task of kind 'acquaintance'.

    Args:
        params (TaskIdInput): task_id (int)

    Returns:
        str: JSON with acknowledgement status per employee.
    """
    try:
        data = await _request("GET", f"/v1/tasks/{params.task_id}/acquaintance-sheet")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


# ===========================================================================
# DIRECTORIES (custom reference lists, e.g. assets/contacts catalogs)
# ===========================================================================

class ListDirectoriesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    parent: Optional[int] = Field(default=None, description="Parent directory ID. Use 0 for root directories.")
    search: Optional[str] = Field(default=None, description="Search by directory title")
    id: Optional[List[int]] = Field(default=None, description="Filter by directory IDs")
    manager: Optional[List[int]] = Field(default=None, description="Filter by manager employee IDs")


@mcp.tool(
    name="upservice_list_directories",
    annotations={"title": "List Upservice Directories", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_list_directories(params: ListDirectoriesInput) -> str:
    """List custom directories (reference catalogs, e.g. assets, contacts) defined in the account.

    Note: this endpoint has no pagination in the Upservice API; it returns all matching directories.

    Args:
        params (ListDirectoriesInput): parent, search, id, manager (all optional)

    Returns:
        str: JSON list of directories.
    """
    try:
        data = await _request("GET", "/v1/directories", params=params.model_dump())
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class CreateDirectoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="Directory title", max_length=255)
    manager_id: int = Field(..., description="Employee ID of the directory manager")
    parent_id: Optional[int] = Field(default=None, description="Parent directory ID, for nested directories")


@mcp.tool(
    name="upservice_create_directory",
    annotations={"title": "Create Upservice Directory", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_create_directory(params: CreateDirectoryInput) -> str:
    """Create a new custom directory (reference catalog).

    Args:
        params (CreateDirectoryInput): title, manager_id (int), parent_id (optional int)

    Returns:
        str: JSON of the created directory.
    """
    try:
        body = {"title": params.title, "manager_id": params.manager_id, "parent_id": params.parent_id}
        data = await _request("POST", "/v1/directories", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class DirectoryIdInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directory_id: int = Field(..., description="The Upservice directory ID")


@mcp.tool(
    name="upservice_get_directory",
    annotations={"title": "Get Upservice Directory", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_directory(params: DirectoryIdInput) -> str:
    """Retrieve a single directory by ID.

    Args:
        params (DirectoryIdInput): directory_id (int)

    Returns:
        str: JSON of the directory record.
    """
    try:
        data = await _request("GET", f"/v1/directories/{params.directory_id}")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class UpdateDirectoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    directory_id: int = Field(..., description="The Upservice directory ID to update")
    title: Optional[str] = Field(default=None, description="New directory title")
    manager_id: Optional[int] = Field(default=None, description="New manager employee ID")


@mcp.tool(
    name="upservice_update_directory",
    annotations={"title": "Update Upservice Directory", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_update_directory(params: UpdateDirectoryInput) -> str:
    """Update a directory's title or manager.

    Args:
        params (UpdateDirectoryInput): directory_id (int), title (optional), manager_id (optional)

    Returns:
        str: JSON of the updated directory.
    """
    try:
        body = {"title": params.title, "manager_id": params.manager_id}
        data = await _request("PUT", f"/v1/directories/{params.directory_id}", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_delete_directory",
    annotations={"title": "Delete Upservice Directory", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_delete_directory(params: DirectoryIdInput) -> str:
    """Permanently delete a directory (and, depending on account settings, its records). Destructive operation.

    Args:
        params (DirectoryIdInput): directory_id (int)

    Returns:
        str: JSON confirmation, or "Error: ..." on failure.
    """
    try:
        data = await _request("DELETE", f"/v1/directories/{params.directory_id}")
        return _ok(data) if data else "Directory deleted successfully."
    except Exception as e:
        return _handle_api_error(e)


# ---- Directory records ----

class ListDirectoryRecordsInput(PaginationInput):
    category: Optional[List[int]] = Field(default=None, description="Filter by directory (category) ID(s)")
    responsible: Optional[List[int]] = Field(default=None, description="Filter by responsible employee ID(s)")
    creator: Optional[List[int]] = Field(default=None, description="Filter by creator employee ID(s)")
    search: Optional[str] = Field(default=None, description="Search by record title or content")
    id: Optional[List[int]] = Field(default=None, description="Filter by record IDs")
    tags_ids: Optional[List[str]] = Field(default=None, description="Filter by one or more tag UUIDs")
    tags_condition: Optional[TagsCondition] = Field(default=None, description="How to combine tags_ids: contains_any, contains_all, not_contain_all, untagged, any")
    date_end_gte: Optional[str] = Field(default=None, description="Expiration date (from), ISO 8601")
    date_end_lte: Optional[str] = Field(default=None, description="Expiration date (to), ISO 8601")
    is_subscribed: Optional[bool] = Field(default=None, description="Filter by subscription status")


@mcp.tool(
    name="upservice_list_directory_records",
    annotations={"title": "List Upservice Directory Records", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_list_directory_records(params: ListDirectoryRecordsInput) -> str:
    """List records within directories, optionally filtered to one or more directories.

    Args:
        params (ListDirectoryRecordsInput): limit, offset, category, responsible, creator, search,
            id, tags_ids, tags_condition, date_end_gte/lte, is_subscribed (all optional)

    Returns:
        str: JSON list of directory records.
    """
    try:
        p = params.model_dump(exclude={"tags_condition"})
        if params.tags_condition:
            p["tags_condition"] = params.tags_condition.value
        data = await _request("GET", "/v1/directory-records", params=p)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class CreateDirectoryRecordInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="Record title", max_length=250)
    category: int = Field(..., description="Directory ID this record belongs to")
    responsible: int = Field(..., description="Responsible employee ID")
    description: Optional[str] = Field(default=None, description="Record description (plain text)")
    inventory_number: Optional[str] = Field(default=None, description="Inventory number, if applicable", max_length=25)
    extra_fields: Optional[Dict[str, Any]] = Field(default=None, description="Additional raw fields to merge into the request body")


@mcp.tool(
    name="upservice_create_directory_record",
    annotations={"title": "Create Upservice Directory Record", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_create_directory_record(params: CreateDirectoryRecordInput) -> str:
    """Create a new record within a directory (e.g. an asset or contact entry).

    Args:
        params (CreateDirectoryRecordInput): title, category (directory ID), responsible (employee ID),
            description (optional), inventory_number (optional), extra_fields (optional dict)

    Returns:
        str: JSON of the created record.
    """
    try:
        body = params.model_dump(exclude={"extra_fields"})
        if params.extra_fields:
            body.update(params.extra_fields)
        data = await _request("POST", "/v1/directory-records", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class RecordIdInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: int = Field(..., description="The Upservice directory record ID")


@mcp.tool(
    name="upservice_get_directory_record",
    annotations={"title": "Get Upservice Directory Record", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_directory_record(params: RecordIdInput) -> str:
    """Retrieve a single directory record by ID.

    Args:
        params (RecordIdInput): record_id (int)

    Returns:
        str: JSON of the record.
    """
    try:
        data = await _request("GET", f"/v1/directory-records/{params.record_id}")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class UpdateDirectoryRecordInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    record_id: int = Field(..., description="The Upservice directory record ID to update")
    title: Optional[str] = Field(default=None, description="New title")
    responsible: Optional[int] = Field(default=None, description="New responsible employee ID. Omit to leave unchanged; use null to clear.")
    description: Optional[str] = Field(default=None, description="New description")
    extra_fields: Optional[Dict[str, Any]] = Field(default=None, description="Additional raw fields to merge into the request body")


@mcp.tool(
    name="upservice_update_directory_record",
    annotations={"title": "Update Upservice Directory Record", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_update_directory_record(params: UpdateDirectoryRecordInput) -> str:
    """Partially update a directory record's fields.

    Args:
        params (UpdateDirectoryRecordInput): record_id (int) plus any fields to change, extra_fields for the rest

    Returns:
        str: JSON of the updated record.
    """
    try:
        body = params.model_dump(exclude={"record_id", "extra_fields"})
        if params.extra_fields:
            body.update(params.extra_fields)
        data = await _request("PATCH", f"/v1/directory-records/{params.record_id}", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_delete_directory_record",
    annotations={"title": "Delete Upservice Directory Record", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_delete_directory_record(params: RecordIdInput) -> str:
    """Permanently delete a directory record. Destructive operation.

    Args:
        params (RecordIdInput): record_id (int)

    Returns:
        str: JSON confirmation, or "Error: ..." on failure.
    """
    try:
        data = await _request("DELETE", f"/v1/directory-records/{params.record_id}")
        return _ok(data) if data else "Directory record deleted successfully."
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="upservice_list_directory_record_relations",
    annotations={"title": "List Upservice Directory Record Relations", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_list_directory_record_relations(params: RecordIdInput) -> str:
    """List the entities (tasks, orders, projects, etc.) linked/related to a directory record.

    Args:
        params (RecordIdInput): record_id (int)

    Returns:
        str: JSON list of related entities, grouped by relation type.
    """
    try:
        data = await _request("GET", f"/v1/directory-records/{params.record_id}/relations")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class RelationChangeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rel_type: DirectoryRelationType = Field(..., description="Type of related entity: order, order_status, contact, project, task, channel_chat, asset, request, or ticket")
    relation_ids: List[Union[int, str]] = Field(..., description="IDs of the related entities to add/remove", min_length=1)
    is_delete: bool = Field(default=False, description="If true, removes these relations instead of adding them")


class BulkUpdateRelationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: int = Field(..., description="The Upservice directory record ID")
    relations: List[RelationChangeItem] = Field(..., description="List of relation changes to apply (add or remove)", min_length=1)


@mcp.tool(
    name="upservice_bulk_update_directory_relations",
    annotations={"title": "Bulk Update Upservice Directory Record Relations", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_bulk_update_directory_relations(params: BulkUpdateRelationsInput) -> str:
    """Add or remove multiple links between a directory record and other entities (tasks, projects, orders, etc.) in one call.

    Args:
        params (BulkUpdateRelationsInput): record_id (int), relations (list of {rel_type, relation_ids, is_delete})

    Returns:
        str: JSON confirmation of the applied relation changes.
    """
    try:
        body = {"relations": [r.model_dump(mode="json") for r in params.relations]}
        for r in body["relations"]:
            r["rel_type"] = r["rel_type"]
        data = await _request("POST", f"/v1/directory-records/{params.record_id}/relations/bulk-update", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


# ===========================================================================
# CHANNELS / MESSAGES / FILES
# ===========================================================================

class ListChatMessagesInput(PaginationInput):
    chat_uuids: List[str] = Field(..., description="One or more chat room UUIDs to load messages for", min_length=1)
    channel_id: str = Field(..., description="Messenger channel UUID (internal channel id) that the chat rooms belong to")
    thread_ids: Optional[List[str]] = Field(default=None, description="Filter to specific thread IDs within the chats")
    language: Optional[str] = Field(default=None, description="Language code for localized message rendering, if supported")
    sender_id: Optional[int] = Field(default=None, description="Filter to messages from a specific sender/employee ID")
    message_kind: Optional[str] = Field(default=None, description="Filter by message kind, if supported by the account")


@mcp.tool(
    name="upservice_list_chat_messages",
    annotations={"title": "List Upservice Chat Messages", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_list_chat_messages(params: ListChatMessagesInput) -> str:
    """Load messages from one or more Upservice chat rooms.

    Args:
        params (ListChatMessagesInput): chat_uuids (required list), channel_id (required), limit, offset, thread_ids, language, sender_id, message_kind (all optional)

    Returns:
        str: JSON list of messages.
    """
    try:
        data = await _request("GET", "/v1/channels/messages", params=params.model_dump())
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class GetChatMessagesInput(PaginationInput):
    channel_unique_identifier: str = Field(..., description="The channel's unique identifier")
    room_uuid: str = Field(..., description="The chat room UUID within the channel")


@mcp.tool(
    name="upservice_get_chat_messages",
    annotations={"title": "Get Upservice Channel Chat Messages", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_chat_messages(params: GetChatMessagesInput) -> str:
    """Get messages for a specific chat room within a specific channel.

    Args:
        params (GetChatMessagesInput): channel_unique_identifier (str), room_uuid (str), limit, offset

    Returns:
        str: JSON list of messages in that room.
    """
    try:
        data = await _request(
            "GET",
            f"/v1/channels/messages/{params.channel_unique_identifier}/chat/{params.room_uuid}/",
            params={"limit": params.limit, "offset": params.offset},
        )
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class SendMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    content: str = Field(..., description=f"Plain-text message content to send. {MENTION_HINT}")
    message_id: Optional[str] = Field(default=None, description="Optional client-supplied UUID for the message (for idempotency/de-duplication)")


@mcp.tool(
    name="upservice_create_external_message",
    annotations={"title": "Create Upservice External Message", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_create_external_message(params: SendMessageInput) -> str:
    """Create a new external/inbound message (e.g. from an external channel integration).

    Args:
        params (SendMessageInput): content (str), message_id (optional UUID str)

    Returns:
        str: JSON of the created message.
    """
    try:
        body = {"content": params.content, "message_id": params.message_id}
        data = await _request("POST", "/v1/channels/messages", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class SendChannelMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel_unique_identifier: str = Field(..., description="The channel's unique identifier (UUID)")
    content: str = Field(..., description=f"Plain-text message content to send. {MENTION_HINT}")
    message_id: Optional[str] = Field(default=None, description="Optional client-supplied UUID for the message (for idempotency/de-duplication)")
    first_name: Optional[str] = Field(default=None, description="Sender first name, for channels that surface external sender identity")
    last_name: Optional[str] = Field(default=None, description="Sender last name")
    email: Optional[str] = Field(default=None, description="Sender email")
    phone: Optional[str] = Field(default=None, description="Sender phone")
    mentions: Optional[List[MentionInput]] = Field(default=None, description="Employees to mention in `content`. Put a `{{employee_id}}` placeholder in the content text for each mention; it will be substituted with the correct @[Name](id) syntax.")
    extra_fields: Optional[Dict[str, Any]] = Field(default=None, description="Additional raw fields (e.g. files) to merge into the request body")


@mcp.tool(
    name="upservice_send_channel_message",
    annotations={"title": "Send Upservice Channel Message", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_send_channel_message(params: SendChannelMessageInput) -> str:
    """Create a new message directly in a channel (not tied to a specific chat room).

    Args:
        params (SendChannelMessageInput): channel_unique_identifier (str), content (str),
            message_id, first_name, last_name, email, phone, mentions (all optional), extra_fields (optional dict)

    Returns:
        str: JSON of the created message.
    """
    try:
        body = params.model_dump(exclude={"channel_unique_identifier", "extra_fields", "mentions"})
        body["content"] = _apply_mentions(params.content, params.mentions)
        if params.extra_fields:
            body.update(params.extra_fields)
        data = await _request("POST", f"/v1/channels/messages/{params.channel_unique_identifier}/", json_body=body)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class SendChatMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel_id: str = Field(..., description="The channel unique identifier to send into")
    room_uuid: str = Field(..., description="The chat room UUID to send the message into")
    content: str = Field(..., description=f"Plain-text message content to send. {MENTION_HINT}")
    message_id: Optional[str] = Field(default=None, description="Optional client-supplied UUID for the message")
    mentions: Optional[List[MentionInput]] = Field(default=None, description="Employees to mention in `content`. Put a `{{employee_id}}` placeholder in the content text for each mention; it will be substituted with the correct @[Name](id) syntax.")


@mcp.tool(
    name="upservice_send_chat_message",
    annotations={"title": "Send Upservice Chat Message", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_send_chat_message(params: SendChatMessageInput) -> str:
    """Send a message into a specific chat room within a channel.

    Args:
        params (SendChatMessageInput): channel_id (str), room_uuid (str), content (str), message_id (optional), mentions (optional)

    Returns:
        str: JSON of the sent message.
    """
    try:
        body = {"content": _apply_mentions(params.content, params.mentions), "message_id": params.message_id}
        data = await _request(
            "POST", f"/v1/channels/messages/{params.channel_id}/chat/{params.room_uuid}/", json_body=body
        )
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class FileIdInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str = Field(..., description="The Upservice file ID (from an attachment or upload)")


@mcp.tool(
    name="upservice_get_file_url",
    annotations={"title": "Get Upservice File Download URL", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_file_url(params: FileIdInput) -> str:
    """Get a fresh, time-limited download URL for a file/attachment.

    Args:
        params (FileIdInput): file_id (str)

    Returns:
        str: JSON containing the file's download URL.
    """
    try:
        data = await _request("GET", f"/v1/files/{params.file_id}/url")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class UploadFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str = Field(..., description="Absolute local filesystem path of the file to upload")


@mcp.tool(
    name="upservice_upload_file",
    annotations={"title": "Upload Upservice File", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_upload_file(params: UploadFileInput) -> str:
    """Upload a local file to Upservice (e.g. to attach it to a task via its returned file ID).

    Args:
        params (UploadFileInput): file_path (absolute local path)

    Returns:
        str: JSON of the uploaded file record, including its ID.
    """
    try:
        data = await _upload_file("/v1/files/", params.file_path)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class UploadFileToChannelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_unique_identifier: str = Field(..., description="The channel's unique identifier (UUID)")
    file_path: str = Field(..., description="Absolute local filesystem path of the file to upload")


@mcp.tool(
    name="upservice_upload_file_to_channel",
    annotations={"title": "Upload Upservice File To External Channel", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def upservice_upload_file_to_channel(params: UploadFileToChannelInput) -> str:
    """Upload a local file to an external channel in Upservice.

    Args:
        params (UploadFileToChannelInput): channel_unique_identifier (str), file_path (absolute local path)

    Returns:
        str: JSON of the uploaded file record, including its ID.
    """
    try:
        data = await _upload_file(f"/v1/files/{params.channel_unique_identifier}/", params.file_path)
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


class ChannelFileIdInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_unique_identifier: str = Field(..., description="The channel's unique identifier (UUID)")
    file_id: str = Field(..., description="The Upservice file ID (from an attachment or upload)")


@mcp.tool(
    name="upservice_get_channel_file_url",
    annotations={"title": "Get Upservice Channel File Download URL", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def upservice_get_channel_file_url(params: ChannelFileIdInput) -> str:
    """Get a fresh, time-limited download URL for a file/attachment scoped to an external channel.

    Args:
        params (ChannelFileIdInput): channel_unique_identifier (str), file_id (str)

    Returns:
        str: JSON containing the file's download URL.
    """
    try:
        data = await _request("GET", f"/v1/files/{params.channel_unique_identifier}/{params.file_id}/url")
        return _ok(data)
    except Exception as e:
        return _handle_api_error(e)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
