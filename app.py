import json
import os
import re
import time
import traceback
from typing import Dict, List, Tuple

import pyodbc
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import OpenAI

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
if not OPENAI_API_KEY:
    raise EnvironmentError("OPENAI_API_KEY is required. Add it to your environment or .env file.")

DB_SERVER = os.getenv("DB_SERVER", "localhost").strip()
DB_NAME = os.getenv("DB_NAME", "AdventureWorksDW2022").strip()
DB_USER = os.getenv("DB_USER", "").strip()
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_DRIVER = os.getenv("DB_DRIVER", "ODBC Driver 18 for SQL Server").strip("{} ")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")

SESSION_TTL = int(os.getenv("SESSION_TTL", "3600"))
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "200"))
MAX_RETRY = int(os.getenv("MAX_RETRY", "3"))
MAX_ROWS = int(os.getenv("MAX_ROWS", "5000"))

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
llm_client = OpenAI(api_key=OPENAI_API_KEY)

_schema_cache: Dict[str, dict] = {}
_schema_text = ""
chat_sessions: Dict[str, dict] = {}


def get_connection():
    """Create a SQL Server connection using SQL auth or Windows auth."""
    base = (
        f"DRIVER={{{DB_DRIVER}}};"
        f"SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};"
        "TrustServerCertificate=yes;"
    )
    if DB_USER and DB_PASSWORD:
        conn_str = base + f"UID={DB_USER};PWD={DB_PASSWORD};"
    else:
        conn_str = base + "Trusted_Connection=yes;"
    return pyodbc.connect(conn_str, timeout=15)


def test_connection() -> dict:
    try:
        with get_connection() as conn:
            conn.cursor().execute("SELECT 1")
        return {"status": "ok", "message": f"Connected to {DB_NAME} on {DB_SERVER}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _build_schema_text(schema: dict) -> str:
    lines = []
    for table, info in schema.items():
        cols = ", ".join(
            f"{column['name']}({column['type']}{' PK' if column['name'] in info['primary_keys'] else ''})"
            for column in info["columns"]
        )
        foreign_keys = "; ".join(
            f"{fk['column']} -> {fk['ref_table']}.{fk['ref_col']}"
            for fk in info["foreign_keys"]
        )
        line = f"  {table}: {cols}"
        if foreign_keys:
            line += f"\n    FK: {foreign_keys}"
        lines.append(line)
    return "\n".join(lines)


def extract_schema() -> dict:
    """Read SQL Server tables, columns, primary keys, and foreign keys."""
    global _schema_cache, _schema_text
    schema = {}

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT t.TABLE_SCHEMA, t.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE,
                       c.IS_NULLABLE,
                       COLUMNPROPERTY(
                           OBJECT_ID(t.TABLE_SCHEMA + '.' + t.TABLE_NAME),
                           c.COLUMN_NAME, 'IsIdentity'
                       ) AS IS_IDENTITY
                FROM INFORMATION_SCHEMA.TABLES t
                JOIN INFORMATION_SCHEMA.COLUMNS c
                  ON t.TABLE_NAME = c.TABLE_NAME
                 AND t.TABLE_SCHEMA = c.TABLE_SCHEMA
                WHERE t.TABLE_TYPE = 'BASE TABLE'
                ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME, c.ORDINAL_POSITION
                """
            )
            for table_schema, table_name, column, dtype, nullable, is_identity in cursor.fetchall():
                key = f"{table_schema}.{table_name}"
                schema.setdefault(key, {"columns": [], "primary_keys": [], "foreign_keys": []})
                schema[key]["columns"].append(
                    {
                        "name": column,
                        "type": dtype,
                        "nullable": nullable == "YES",
                        "identity": bool(is_identity),
                    }
                )

            cursor.execute(
                """
                SELECT tc.TABLE_SCHEMA, tc.TABLE_NAME, kcu.COLUMN_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                 AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
                WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                """
            )
            for table_schema, table_name, column in cursor.fetchall():
                key = f"{table_schema}.{table_name}"
                if key in schema:
                    schema[key]["primary_keys"].append(column)

            cursor.execute(
                """
                SELECT tp.name AS parent_table, cp.name AS parent_col,
                       tr.name AS ref_table, cr.name AS ref_col,
                       SCHEMA_NAME(tp.schema_id) AS parent_schema
                FROM sys.foreign_keys fk
                JOIN sys.foreign_key_columns fkc
                  ON fk.object_id = fkc.constraint_object_id
                JOIN sys.tables tp ON fkc.parent_object_id = tp.object_id
                JOIN sys.columns cp ON fkc.parent_object_id = cp.object_id
                                   AND fkc.parent_column_id = cp.column_id
                JOIN sys.tables tr ON fkc.referenced_object_id = tr.object_id
                JOIN sys.columns cr ON fkc.referenced_object_id = cr.object_id
                                   AND fkc.referenced_column_id = cr.column_id
                """
            )
            for p_table, p_col, r_table, r_col, p_schema in cursor.fetchall():
                key = f"{p_schema}.{p_table}"
                if key in schema:
                    schema[key]["foreign_keys"].append(
                        {"column": p_col, "ref_table": r_table, "ref_col": r_col}
                    )

        _schema_cache = schema
        _schema_text = _build_schema_text(schema)
        print(f"[INFO] Schema loaded: {len(schema)} tables")
    except Exception as exc:
        print(f"[WARN] Schema extraction failed: {exc}")
        _schema_cache = {}
        _schema_text = "Schema unavailable. Check the database connection."

    return schema


SYNONYM_HINT = """\
revenue/sales/income/earnings/turnover are synonyms
customer/client/buyer/account/user are synonyms
employee/staff/worker/personnel are synonyms
order/purchase/transaction/booking are synonyms
amount/value/total/sum/price/cost are synonyms
quantity/qty/count/units are synonyms
profit/margin/gain/net are synonyms
region/area/territory/zone/location are synonyms
"""


def _llm(messages: list, temperature: float = 0.1, max_tokens: int = 2500) -> str:
    response = llm_client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=messages,
    )
    return response.choices[0].message.content.strip()


def _strip_code_fences(text: str) -> str:
    return re.sub(r"```(?:json|sql)?\s*|\s*```", "", text, flags=re.IGNORECASE).strip()


def nl_to_sql(question: str, history: List[dict]) -> dict:
    standalone = question
    if history:
        history_text = "\n".join(
            f"{message['role'].upper()}: {message['content']}" for message in history[-6:]
        )
        standalone = _llm(
            [
                {
                    "role": "system",
                    "content": (
                        "Rewrite follow-up data questions as standalone questions. "
                        "Return only the rewritten question."
                    ),
                },
                {
                    "role": "user",
                    "content": f"History:\n{history_text}\n\nFollow-up: {question}",
                },
            ],
            max_tokens=200,
        )

    system_prompt = f"""
You are an expert Microsoft SQL Server data analyst.

DATABASE SCHEMA:
{_schema_text or 'Schema not loaded.'}

DOMAIN SYNONYMS:
{SYNONYM_HINT}

RULES:
- Generate a single read-only SELECT query. A CTE beginning with WITH is allowed.
- Use only tables and columns shown in the schema.
- Use foreign-key relationships where appropriate.
- Never generate INSERT, UPDATE, DELETE, MERGE, DROP, ALTER, CREATE, TRUNCATE,
  EXEC/EXECUTE, stored procedures, dynamic SQL, OPENROWSET, BULK INSERT, or XP_ calls.
- Do not use temp tables or SELECT ... INTO.
- Prefer concise queries and meaningful ordering.
- Keep result sets reasonably bounded when the request could return very large data.

Return valid JSON only, with this exact shape:
{{
  "intent": "what the user wants",
  "entities": {{"dates": [], "metrics": [], "filters": [], "tables": []}},
  "sql": "SELECT ...",
  "explanation": "plain-English explanation",
  "confidence": 0.95,
  "warnings": []
}}
""".strip()

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-4:])
    messages.append({"role": "user", "content": standalone})
    raw = _llm(messages, temperature=0.05)

    try:
        return json.loads(_strip_code_fences(raw))
    except Exception:
        sql_match = re.search(r"\b(?:WITH|SELECT)\b.+", raw, re.DOTALL | re.IGNORECASE)
        return {
            "intent": "Query",
            "entities": {},
            "sql": sql_match.group(0).strip() if sql_match else "",
            "explanation": raw,
            "confidence": 0.5,
            "warnings": ["The model response was not valid JSON; review the generated SQL."],
        }


def _remove_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--.*?$", " ", sql, flags=re.MULTILINE)
    return sql.strip()


def validate_sql(sql: str) -> Tuple[bool, str]:
    """Apply a conservative read-only allowlist before executing generated SQL."""
    if not sql or not sql.strip():
        return False, "Empty SQL query."

    cleaned = _remove_sql_comments(sql).strip()
    if not cleaned:
        return False, "Empty SQL query."

    # Allow one optional trailing semicolon, but reject multiple statements.
    without_trailing = cleaned[:-1].rstrip() if cleaned.endswith(";") else cleaned
    if ";" in without_trailing:
        return False, "Multiple SQL statements are not allowed."

    if not re.match(r"^(SELECT\b|WITH\b)", without_trailing, flags=re.IGNORECASE):
        return False, "Only read-only SELECT queries are permitted."

    blocked_patterns = [
        r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b", r"\bMERGE\b",
        r"\bDROP\b", r"\bALTER\b", r"\bCREATE\b", r"\bTRUNCATE\b",
        r"\bEXEC(?:UTE)?\b", r"\bOPENROWSET\b", r"\bOPENDATASOURCE\b",
        r"\bBULK\s+INSERT\b", r"\bXP_\w*\b", r"\bSP_\w*\b",
        r"\bINTO\s+#?\w+\b", r"\bSELECT\b[\s\S]*?\bINTO\b",
    ]
    for pattern in blocked_patterns:
        if re.search(pattern, without_trailing, flags=re.IGNORECASE):
            return False, "The generated query contains a blocked SQL operation."

    return True, "OK"


def execute_sql(sql: str) -> dict:
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            if not cursor.description:
                return {"success": True, "columns": [], "rows": [], "count": 0, "capped": False}

            columns = [description[0] for description in cursor.description]
            rows = [
                [str(value) if value is not None else None for value in row]
                for row in cursor.fetchmany(MAX_ROWS)
            ]
            return {
                "success": True,
                "columns": columns,
                "rows": rows,
                "count": len(rows),
                "capped": len(rows) == MAX_ROWS,
            }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def self_heal(original_sql: str, error: str, attempt: int) -> str:
    fixed = _llm(
        [
            {
                "role": "system",
                "content": (
                    "Fix the Microsoft SQL Server query using only the supplied schema. "
                    "The result must remain a single read-only SELECT/CTE query. "
                    "Return SQL only.\n\nSCHEMA:\n" + _schema_text
                ),
            },
            {
                "role": "user",
                "content": (
                    f"BROKEN SQL:\n{original_sql}\n\nERROR: {error}\n\n"
                    f"Attempt {attempt}/{MAX_RETRY}."
                ),
            },
        ],
        temperature=0.0,
        max_tokens=800,
    )
    return _strip_code_fences(fixed)


def generate_insights(sql: str, columns: List[str], rows: List[list]) -> str:
    if not rows:
        return "No data returned."

    sample = rows[:50]
    result_text = "\t".join(columns) + "\n"
    result_text += "\n".join("\t".join(str(value or "") for value in row) for row in sample)
    if len(rows) > 50:
        result_text += f"\n... ({len(rows) - 50} more rows)"

    return _llm(
        [
            {
                "role": "system",
                "content": (
                    "You are a concise business analyst. Give up to two evidence-based insights "
                    "from the query result. Reference actual values and do not invent conclusions."
                ),
            },
            {
                "role": "user",
                "content": f"SQL:\n{sql}\n\nResults ({len(rows)} rows):\n{result_text}",
            },
        ],
        temperature=0.2,
        max_tokens=220,
    )


def _evict_sessions() -> None:
    now = time.time()
    expired = [sid for sid, value in chat_sessions.items() if now - value["last_used"] > SESSION_TTL]
    for sid in expired:
        chat_sessions.pop(sid, None)

    if len(chat_sessions) > MAX_SESSIONS:
        lru = sorted(chat_sessions.items(), key=lambda item: item[1]["last_used"])
        for sid, _ in lru[: len(chat_sessions) - MAX_SESSIONS]:
            chat_sessions.pop(sid, None)


def get_history(session_id: str) -> List[dict]:
    _evict_sessions()
    session = chat_sessions.setdefault(session_id, {"history": [], "last_used": time.time()})
    session["last_used"] = time.time()
    return session["history"]


def save_turn(session_id: str, user_message: str, assistant_message: str) -> None:
    history = get_history(session_id)
    history.extend(
        [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ]
    )
    chat_sessions[session_id]["history"] = history[-20:]


@app.route("/")
def home():
    return render_template("index.html", tables=list(_schema_cache.keys()), db_name=DB_NAME)


@app.route("/query", methods=["POST"])
def query():
    data = request.get_json(silent=True) or {}
    question = str(data.get("question", "")).strip()
    session_id = str(data.get("session_id", "default"))[:128]
    run_query = bool(data.get("run_query", True))

    if not question:
        return jsonify({"error": "question is required"}), 400

    try:
        result = nl_to_sql(question, get_history(session_id))
        original_sql = str(result.get("sql", "")).strip()
        valid, reason = validate_sql(original_sql)
        if not valid:
            return jsonify(
                {
                    **result,
                    "error": reason,
                    "columns": [],
                    "rows": [],
                    "insights": "",
                }
            )

        execution = {"success": False, "error": "Query execution was skipped."}
        final_sql = original_sql
        retry_log = []

        if run_query:
            for attempt in range(1, MAX_RETRY + 1):
                valid, reason = validate_sql(final_sql)
                if not valid:
                    execution = {"success": False, "error": reason}
                    break

                execution = execute_sql(final_sql)
                if execution.get("success"):
                    break

                error = execution.get("error", "Unknown SQL error")
                retry_log.append(f"Attempt {attempt}: {error}")
                if attempt < MAX_RETRY:
                    repaired = self_heal(final_sql, error, attempt)
                    repaired_valid, _ = validate_sql(repaired)
                    if repaired and repaired != final_sql and repaired_valid:
                        final_sql = repaired
                    else:
                        break

        insights = ""
        if execution.get("success") and execution.get("rows"):
            insights = generate_insights(final_sql, execution["columns"], execution["rows"])

        save_turn(session_id, question, result.get("explanation", original_sql[:120]))

        return jsonify(
            {
                "intent": result.get("intent", ""),
                "entities": result.get("entities", {}),
                "sql": final_sql,
                "original_sql": original_sql if final_sql != original_sql else None,
                "explanation": result.get("explanation", ""),
                "confidence": result.get("confidence", 0),
                "warnings": list(result.get("warnings", [])) + retry_log,
                "columns": execution.get("columns", []),
                "rows": execution.get("rows", []),
                "count": execution.get("count", 0),
                "capped": execution.get("capped", False),
                "insights": insights,
                "error": execution.get("error") if not execution.get("success") else None,
            }
        )
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": "The request could not be completed.", "detail": str(exc)}), 500


@app.route("/schema")
def schema_route():
    return jsonify({"tables": _schema_cache, "text": _schema_text})


@app.route("/schema/refresh", methods=["POST"])
def refresh_schema():
    extract_schema()
    return jsonify({"status": "ok", "tables": len(_schema_cache)})


@app.route("/db/test")
def db_test():
    return jsonify(test_connection())


@app.route("/session/clear", methods=["POST"])
def clear_session():
    data = request.get_json(silent=True) or {}
    chat_sessions.pop(str(data.get("session_id", "default"))[:128], None)
    return jsonify({"status": "cleared"})


if __name__ == "__main__":
    print("[INFO] Loading database schema...")
    extract_schema()
    print("[INFO] Ready at http://localhost:5010")
    app.run(host="127.0.0.1", port=5010, debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
