# REST API Design

REST APIs model resources as nouns (`/notes/12`) and use HTTP verbs for actions: GET reads, POST creates, PUT replaces, PATCH updates, DELETE removes. Return meaningful status codes (200, 201, 400, 404, 422, 500) and consistent JSON errors. Version the API and validate every input at the boundary. Long-running work should return quickly and report progress; see [[Async Programming]]. Test endpoints with [[Unit Testing with Pytest]].
