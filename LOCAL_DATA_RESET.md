# Reset local development data

The Compose volumes contain PostgreSQL contact, consent, campaign, and audit records, plus Redis data. The reset below permanently deletes those local volumes. Do not run it if you need the current local database.

From the project root:

```powershell
docker compose down
docker compose down -v
docker compose up -d postgres redis
```

Then from `backend`, reapply the schema with `alembic upgrade head`. This procedure is for local synthetic development data only. For any environment containing real contacts, use the approved backup/restore and retention process instead.
