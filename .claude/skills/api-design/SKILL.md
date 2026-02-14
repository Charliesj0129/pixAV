---
name: api-design
description: REST API design patterns including resource naming, status codes, pagination, and error handling.
---

# API Design Patterns

Conventions for designing consistent REST APIs in `pixAV` (FastAPI).

## Resource Design

- **URLs**: Nouns, plural, kebab-case (e.g., `/api/v1/tasks`).
- **Hierarchy**: Nest resources for ownership (`/api/v1/users/:id/orders`).
- **Methods**: proper use of GET, POST, PUT, DELETE, PATCH.

## Status Codes

- **200 OK**: Success.
- **201 Created**: Resource created (return `Location` header).
- **204 No Content**: Successful delete/update with no body.
- **400 Bad Request**: Validation failure.
- **401 Unauthorized**: Missing/invalid token.
- **403 Forbidden**: Valid token, insufficient permissions.
- **404 Not Found**: Resource does not exist.
- **422 Unprocessable**: Pydantic validation error.
- **500 Internal Error**: Unexpected failure.

## Response Format

```json
{
  "data": { ... },
  "meta": { "page": 1, "total": 100 }
}
```

## Pagination

- Use **Cursor-based** for high-volume data (feeds).
- Use **Offset-based** (`page`, `limit`) for admin tables.

## Implementation (FastAPI)

- **Models**: Use Pydantic models for Request/Response schemas.
- **Dependencies**: Use `Depends()` for auth and db sessions.
- **Async**: All route handlers should be `async def`.
- **Tags**: Organize routes with tags for Swagger UI.

## Error Handling

- Raise `HTTPException`.
- Return detailed error codes (e.g., `TASK_NOT_FOUND`) in body.
