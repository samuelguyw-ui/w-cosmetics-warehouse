W Cosmetics Warehouse - Railway Production Build

Database:
- Uses DATABASE_URL when provided (Railway PostgreSQL).
- Falls back to local SQLite only when DATABASE_URL is absent.

Railway:
1. Add DATABASE_URL as a reference to the Postgres service:
   ${{Postgres.DATABASE_URL}}
2. Push this project to GitHub.
3. Railway redeploys automatically.
4. Test /health before configuring the custom domain.


Change the password after first login.

Production note:
The current build keeps uploaded Excel files and generated exports on the service filesystem.
For high-availability production, move these files to S3-compatible object storage or a Railway volume.
