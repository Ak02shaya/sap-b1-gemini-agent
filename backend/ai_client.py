import os
import json
import re
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

import google.generativeai as genai
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from backend.sap_client import execute_sap_query
from backend.rule_enforcer import check_business_rules, is_sql_safe, slice_rows, TENANT_CONFIG

load_dotenv()
logger = logging.getLogger("sap_ai_assistant")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# Initialize Gemini Client
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

class DashboardResponse(BaseModel):
    analysis: str = Field(description="Structured dashboard report formatted strictly with Markdown tables and visual sections.")
    solution_evaluation: str = Field(description="Key observations and analytical takeaway without table names.")
    suggestions: list[str] = Field(description="3 context-aware follow-up business queries.")

def _scrub_markdown_formatting(text: str) -> str:
    if not text: return text
    text = re.sub(r'^(#{1,6})\s+(.+)$', r'**\2**', text, flags=re.MULTILINE)
    text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)
    return text

def _empty_dashboard(analysis: str, evaluation: str, suggestions: list[str], exec_sec: float = 0.0) -> dict:
    return {
        "analysis": _scrub_markdown_formatting(analysis),
        "solution_evaluation": _scrub_markdown_formatting(evaluation),
        "suggestions": suggestions,
        "execution_time_sec": exec_sec,
    }

# Gemini Tool Definitions (Dictionary Format)
TOOLS = [
    {
        "function_declarations": [
            {
                "name": "execute_sap_sql",
                "description": "Executes a single read-only SELECT statement on SAP HANA against the live SAP B1 database. MANDATORY: You must call this tool and receive successful data results before calling submit_dashboard.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "sql_query": {
                            "type": "STRING",
                            "description": "Flat HANA SELECT query starting with SELECT TOP 25. NO WITH clauses."
                        }
                    },
                    "required": ["sql_query"]
                }
            },
            {
                "name": "submit_dashboard",
                "description": "Submits the final structured dashboard report. CALL THIS TOOL ONLY AFTER receiving a successful dataset from execute_sap_sql.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "analysis": {
                            "type": "STRING", 
                            "description": "Structured dashboard report formatted strictly with Markdown tables."
                        },
                        "solution_evaluation": {
                            "type": "STRING", 
                            "description": "Key observations and analytical takeaway without table names."
                        },
                        "suggestions": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                            "description": "3 context-aware follow-up business queries."
                        }
                    },
                    "required": ["analysis", "solution_evaluation", "suggestions"]
                }
            }
        ]
    }
]

def _print_execution_summary(session_state: dict, input_tokens: int, output_tokens: int, exec_time_sec: float):
    total_tokens = input_tokens + output_tokens
    print("\n" + "="*50)
    print("🏆 FINAL EXECUTED SQL QUERY:")
    final_sql = session_state.get("final_executed_sql") or session_state.get("last_attempted_sql") or "No SQL generated."
    print(final_sql)
    print("\n📊 TOKEN USAGE:")
    print(f"   - Input Tokens  : {input_tokens}")
    print(f"   - Output Tokens : {output_tokens}")
    print(f"   - Total Tokens  : {total_tokens}")
    print(f"⏱️ Execution Time : {exec_time_sec} seconds")
    print("="*50 + "\n")

async def handle_agent_tool(tool_name: str, tool_input: dict, company_id: str, session_state: dict) -> str:
    if tool_name == "execute_sap_sql":
        sql = tool_input.get("sql_query", "").strip()
        session_state["last_attempted_sql"] = sql
        
        if not is_sql_safe(sql):
            print(f"\n⚠️ SECURITY REJECTION:\n{sql}\n")
            return "ERROR: Query rejected by security filter. No restricted keywords or DML allowed."
            
        norm_sql, rule_violations = check_business_rules(sql, company_id)
        if rule_violations:
            # THIS PRINTS THE EXACT FIREWALL ERROR TO YOUR TERMINAL
            error_msg = "RULES VIOLATED. Please fix the SQL and retry:\n- " + "\n- ".join(rule_violations)
            print(f"\n⚠️ FIREWALL REJECTED QUERY:\n{error_msg}\n")
            return error_msg

        result = await execute_sap_query(norm_sql, company_id=company_id)
        if isinstance(result, dict) and "error" in result:
            # THIS PRINTS DATABASE ERRORS TO YOUR TERMINAL
            print(f"\n❌ DATABASE ERROR:\n{result['error']}\n")
            return f"DATABASE ERROR: {result['error']}. Fix the query and retry."

        session_state["final_executed_sql"] = norm_sql
        sliced_result = slice_rows(result, 25)
        
        if isinstance(result, list) and len(result) > 25:
            return json.dumps({
                "notice": f"Dataset truncated. Showing 25 of {len(result)} total rows.",
                "data": sliced_result
            }, default=str)
            
        return json.dumps(sliced_result, default=str)

    return "Unknown tool invoked."

async def ask_ai_assistant(
    question: str, 
    company_id: str = "DEFAULT", 
    conversation_history: Optional[list[dict]] = None
) -> dict:
    
    start_time = time.perf_counter()
    session_state = {"final_executed_sql": "", "last_attempted_sql": ""}
    
    session_input_tokens = 0
    session_output_tokens = 0

    now_utc = datetime.now(timezone.utc)
    current_date = now_utc.strftime("%Y-%m-%d")
    current_year = now_utc.year
    
    default_config = TENANT_CONFIG.get("DEFAULT", {"brand_logic": "", "location_logic": "", "ai_prompt_exclusions": []})
    tenant_rules = TENANT_CONFIG.get(company_id, default_config)
    brand_logic = tenant_rules.get("brand_logic", "")
    location_logic = tenant_rules.get("location_logic", "")
    exclusions_list = tenant_rules.get("ai_prompt_exclusions", [])
    exclusions_text = "\n- ".join(exclusions_list) if exclusions_list else "None specific."

    agent_system_prompt = f"""\
ROLE
You are an SAP Business One HANA SQL intelligence agent for database {company_id}.
Today: {current_date} ({current_year}).

DATA
- Business data comes only from the live SAP database through execute_sap_sql.
- Never invent, assume, or fabricate database values.
- Do not use or store previous questions, queries, or results.

SQL
- Generate exactly one read-only SELECT query.
- Query must start with SELECT TOP 25.
- No WITH/CTE, ROUND(), or CAST().
- Use aliases: T0=OINV, T1=INV1, T2=OITM, OWHS=OWHS, OCRD=OCRD.
- For invoice queries, join:
  INNER JOIN OWHS ON T1."WhsCode" = OWHS."WhsCode"
- Never use OINV."U_ReqWhs".
- Use LOWER() on both sides of case-insensitive text filters.
- MANDATORY: You must ALWAYS include an explicit ORDER BY clause (e.g., ORDER BY 2 DESC) if your query contains a GROUP BY or SUM().

BUSINESS RULES
- Active invoices only:
  T0."CANCELED" = 'N' AND T0."DocType" = 'I'
- Revenue must use SUM(T1."LineTotal"), never SUM(T0."DocTotal").
- Exclude sales BOM bundles:
  T1."TreeType" <> 'S'
- Apply every tenant-specific exclusion below.

TENANT RULES
Company: {company_id}
Brand: {brand_logic}
Location: {location_logic}
Exclusions:
- {exclusions_text}

TOOL WORKFLOW
1. Understand the user's question.
2. Generate one SQL query.
3. Call execute_sap_sql.
4. If Python rejects the query or the database returns an error, fix only the reported problem and retry.
5. CRITICAL: Do not generate alternative test queries. If execute_sap_sql succeeds (returns data or an empty list []), you MUST immediately call submit_dashboard in the very next step. 
6. Never try to query SYS tables or debug the database schema if data is empty. Just submit the dashboard stating no data was found.
7. Never call submit_dashboard before successful SQL execution.

OUTPUT
- Use simple business language.
- Do not expose SQL, table names, aliases, or database implementation details.
- Never invent values.
"""

    # Initialize Gemini Model
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        tools=TOOLS,
        system_instruction=agent_system_prompt
    )

    # Translate standard chat history format to Gemini format
    messages = []
    if conversation_history:
        for msg in conversation_history:
            role = "model" if msg.get("role") == "assistant" else "user"
            messages.append({"role": role, "parts": [msg.get("content", "")]})
    
    messages.append({"role": "user", "parts": [question]})
    
    max_steps = 6 

    for step in range(max_steps):
        try:
            response = await model.generate_content_async(
                messages,
                generation_config=genai.types.GenerationConfig(max_output_tokens=2048)
            )

            # Track tokens
            if hasattr(response, "usage_metadata"):
                session_input_tokens += getattr(response.usage_metadata, "prompt_token_count", 0)
                session_output_tokens += getattr(response.usage_metadata, "candidates_token_count", 0)

            if not response.candidates:
                raise ValueError("No response returned from the model.")
                
            model_message = response.candidates[0].content
            messages.append(model_message)

            # Parse Gemini Tool Calls
            function_calls = [part.function_call for part in model_message.parts if part.function_call]

            if function_calls:
                tool_responses = []
                submit_call = None

                for fc in function_calls:
                    if fc.name == "submit_dashboard":
                        submit_call = fc
                        continue
                    
                    # Convert protobuf map to Python dict safely
                    tool_input = type(fc).to_dict(fc).get("args", {})
                    tool_output = await handle_agent_tool(fc.name, tool_input, company_id, session_state)
                    
                    tool_responses.append({
                        "function_response": {
                            "name": fc.name,
                            "response": {"result": tool_output}
                        }
                    })

                if tool_responses:
                    messages.append({"role": "user", "parts": tool_responses})

                if submit_call:
                    if not session_state.get("final_executed_sql"):
                        messages.append({"role": "user", "parts": [{
                            "function_response": {
                                "name": "submit_dashboard",
                                "response": {"error": "Run execute_sap_sql successfully before submitting."}
                            }
                        }]})
                        continue
                        
                    exec_time_sec = round(time.perf_counter() - start_time, 2)
                    args = type(submit_call).to_dict(submit_call).get("args", {})
                    
                    try:
                        result = DashboardResponse.model_validate(args).model_dump()
                    except Exception as format_error:
                        logger.warning(f"Formatting recovery: {format_error}")
                        result = {
                            "analysis": str(args.get("analysis", "Query executed successfully.")),
                            "solution_evaluation": str(args.get("solution_evaluation", "Task completed.")),
                            "suggestions": args.get("suggestions", ["Show sales summary"])
                        }
                        
                    result["analysis"] = _scrub_markdown_formatting(result["analysis"])
                    result["solution_evaluation"] = _scrub_markdown_formatting(result["solution_evaluation"])
                    result["execution_time_sec"] = exec_time_sec
                    
                    _print_execution_summary(session_state, session_input_tokens, session_output_tokens, exec_time_sec)
                    return result
            else:
                # Text-only response fallback
                raw_text = "".join(part.text for part in model_message.parts if hasattr(part, "text"))
                logger.warning(f"Model attempted to chat instead of using a tool: {raw_text}")
                
                if not session_state.get("final_executed_sql") and step < max_steps - 1:
                    messages.append({
                        "role": "user", 
                        "parts": ["ERROR: You must execute an SQL query using `execute_sap_sql`. Do not write conversational text or apologies. Fix your query and call the tool."]
                    })
                    continue
                
                exec_time_sec = round(time.perf_counter() - start_time, 2)
                _print_execution_summary(session_state, session_input_tokens, session_output_tokens, exec_time_sec)
                return _empty_dashboard(raw_text, "Query completed.", ["Show sales summary"], exec_time_sec)

        except Exception as e:
            exec_time_sec = round(time.perf_counter() - start_time, 2)
            logger.error(f"Agent loop failure on step {step}: {e}", exc_info=True)
            _print_execution_summary(session_state, session_input_tokens, session_output_tokens, exec_time_sec)
            return _empty_dashboard(
                f"An unexpected error occurred during execution: {str(e)}",
                "Execution failed.",
                ["Try rephrasing your question"],
                exec_time_sec
            )

    total_time = round(time.perf_counter() - start_time, 2)
    _print_execution_summary(session_state, session_input_tokens, session_output_tokens, total_time)
    return _empty_dashboard(
        "The SQL could not be validated after several attempts. Please try rephrasing your question.",
        "Execution aborted due to strict validation limits.",
        ["Show sales summary"],
        total_time
    )