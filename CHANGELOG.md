# Changelog

## 0.1.1

- Allow snapshots containing SQLite tables or indexes with empty identifiers to be loaded and compared.

## 0.1.0

- Inspect SQLite schema in a read-only transaction and write a bounded JSON snapshot.
- Compare saved snapshots as text or JSON with added, removed and changed objects.
- Include column, foreign-key and index metadata; preserve implicit indexes and SQL definitions.
