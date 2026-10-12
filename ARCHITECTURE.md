# Architecture

```text
User question
   ↓
Flask API (`/query`)
   ↓
Short conversational context
   ↓
Database schema + prompt builder
   ↓
OpenAI model (natural language → SQL)
   ↓
Read-only SQL validator
   ↓
Microsoft SQL Server via pyodbc
   ↓
Optional self-healing retry on SQL errors
   ↓
Result summarization / insights
   ↓
JSON response → browser UI
```

The application inspects table, column, primary-key, and foreign-key metadata so the model can generate schema-aware SQL.
