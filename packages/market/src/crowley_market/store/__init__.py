"""PostgreSQL adapters used by the HTTP server.

Repositories in this package take an already-open connection.  They never
start, commit, or roll back a transaction; the route/application boundary
owns the unit of work for each request.
"""
