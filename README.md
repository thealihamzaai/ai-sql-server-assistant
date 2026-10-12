# AI SQL Server Assistant

A Flask-based natural-language-to-SQL application for Microsoft SQL Server. Users can ask business questions in plain English, review the generated SQL, execute read-only queries, and receive concise AI-generated insights from the results.

## What it demonstrates

- Natural-language-to-SQL generation with OpenAI
- Live SQL Server schema discovery
- Primary-key and foreign-key awareness
- Conversational follow-up questions
- Conservative read-only SQL validation
- Automatic retry/self-repair for failed generated queries
- Result summarization and business insights
- Flask web interface with database/schema visibility

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the request flow.

## Tech stack

- Python
- Flask
- OpenAI API
- Microsoft SQL Server
- `pyodbc`
- Bootstrap-based web UI

## Setup

### 1. Prerequisites

Install:

- Python 3.10+
- Microsoft SQL Server or access to an existing SQL Server instance
- Microsoft ODBC Driver 18 for SQL Server (recommended)

### 2. Install dependencies

```bash
python -m venv .venv
```

Activate the environment and run:

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy `.env.example` to `.env` and provide your own values:

```env
OPENAI_API_KEY=your_openai_api_key
DB_SERVER=localhost
DB_NAME=AdventureWorksDW2022
DB_USER=
DB_PASSWORD=
DB_DRIVER=ODBC Driver 18 for SQL Server
FLASK_SECRET_KEY=replace_with_a_long_random_value
```

Leave `DB_USER` and `DB_PASSWORD` empty if you are using Windows Authentication.

### 4. Run the application

```bash
python app.py
```

Open:

```text
http://localhost:5010
```

## Example questions

- "Show total sales by region."
- "Which products generated the most revenue?"
- "Compare monthly sales for the last two years."
- "Now show only the top five." (follow-up question)

## Safety design

Generated SQL is validated before execution. The app accepts only a single `SELECT` query or a CTE that resolves to a read-only query, and blocks common write/admin operations such as `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `EXEC`, and `SELECT INTO`.

For real production use, **also connect with a database login that has database-level read-only permissions**. Application-side validation should not be treated as the only security boundary.

## Important limitations

- LLM-generated SQL can still be incorrect even when syntactically valid.
- Database schemas and business definitions vary, so generated queries should be reviewed for important decisions.
- In-memory chat sessions are intended for demonstration, not distributed production deployment.
- This repository does not include database credentials, API keys, or proprietary data.

## Security

Never commit `.env`, API keys, database passwords, connection secrets, or client data. The included `.gitignore` excludes local environment files.

## Portfolio note

This project is presented as an applied AI engineering demonstration of schema-aware Text-to-SQL, database interaction, and LLM-assisted analytics—not as a production database governance platform.
