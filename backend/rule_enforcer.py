import re
import json
from pathlib import Path
from typing import List, Tuple, Any

_LITERAL = re.compile(r"'(?:[^']|'')*'")

_FLAG_COLUMNS = {
    "DOCTYPE", "CANCELED", "DOCSTATUS", "LINESTATUS", "TREETYPE",
    "INVNTITEM", "WHSCODE", "ITEMCODE", "CARDCODE", "ITMSGRPCOD",
}

_BLOCKED = [
    r"\bDROP\s+", r"\bDELETE\s+", r"\bUPDATE\s+", r"\bINSERT\s+",
    r"\bALTER\s+", r"\bEXEC(UTE)?\s+", r"\bTRUNCATE\s+", r"\bMERGE\s+",
    r"\bGRANT\s+", r"\bREVOKE\s+", r"\bCREATE\s+", r"\bCALL\s+",
    r"--", r"/\*", r";", r"\bWITH\s+", r"\bINTO\s+", r"\bCROSS\s+JOIN\s+",
    r"\bSYS\.", r"\b_SYS_", r"\bM_\w+\b", r"\bOUSR\b"
]

CONFIG_PATH = Path(__file__).parent / "tenant_config.json"
try:
    with open(CONFIG_PATH, "r") as f:
        TENANT_CONFIG = json.load(f)
except Exception:
    TENANT_CONFIG = {"DEFAULT": {"brand_logic": "", "location_logic": "", "ai_prompt_exclusions": [], "firewall_regex_rules": []}}

def _T(name: str) -> str:
    return rf'(?:"[^"]+"\.)?"?{name}"?'

def _C(alias: str, col: str) -> str:
    return rf'\b{alias}\."?{col}"?'

def _normalise(sql: str) -> str:
    return " ".join(sql.strip().rstrip(";").split())

def is_sql_safe(sql: str) -> bool:
    if not sql or not isinstance(sql, str):
        return False
    code = _LITERAL.sub("''", _normalise(sql))
    if not code.upper().startswith("SELECT"):
        return False
    return not any(re.search(p, code, re.IGNORECASE) for p in _BLOCKED)

def check_business_rules(sql: str, company_id: str) -> Tuple[str, List[str]]:
    v: List[str] = []
    norm_sql = _normalise(sql)
    code_upper = _LITERAL.sub("''", norm_sql).upper()

    # Universal Rules
    if not re.match(r"^SELECT\s+(DISTINCT\s+)?TOP\s+25\b", code_upper):
        v.append("Query must start with SELECT TOP 25.")

    if re.search(r"\bROUND\s*\(", code_upper) or re.search(r"\bCAST\s*\(", code_upper):
        v.append("ROUND() and CAST() are forbidden. Return raw numeric database values.")

    has_agg = re.search(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", code_upper)
    if (has_agg or "GROUP BY" in code_upper) and not re.search(r"\bORDER\s+BY\s+", code_upper):
        v.append("You must include an explicit ORDER BY clause when aggregating data.")

    # Strictly forbid Stock Transfers
    if re.search(r"\b(OWTR|WTR1|OWTQ|WTQ1)\b", code_upper):
        v.append("RULE VIOLATION: Stock transfers are strictly excluded. You must query actual sales invoices.")

    if company_id and company_id.upper() != "DEFAULT":
        schema_matches = re.findall(r'FROM\s+"([^"]+)"\.', code_upper)
        for schema in schema_matches:
            if company_id.upper() not in schema.upper():
                v.append(f"RULE VIOLATION: Cross-schema query to '{schema}' is forbidden.")

    # Field/Schema Rules
    if "U_REQWHS" in code_upper:
        v.append("RULE VIOLATION: Never use header-level U_ReqWhs. You MUST use line-level T1.\"WhsCode\".")
    if "OMRC" in code_upper:
        v.append("RULE VIOLATION: The OMRC table is forbidden. Use OITM.\"U_Brand\" instead.")
    if "U_LOCATION" in code_upper:
        v.append("RULE VIOLATION: U_Location is forbidden. Use OWHS.\"Location\" or OWHS.\"WhsName\".")

    # Invoice specific rules
    if re.search(_T("OINV"), code_upper) or re.search(_T("INV1"), code_upper):
        if not re.search(_T("OINV") + r'\s+(?:AS\s+)?T0\b', code_upper):
            v.append("You must alias OINV as T0.")
        if not re.search(_T("INV1") + r'\s+(?:AS\s+)?T1\b', code_upper):
            v.append("You must join INV1 as T1.")
        
        # ADDED re.IGNORECASE so "DocType" and "DOCTYPE" both pass!
        if not re.search(_C("T0", "DOCTYPE") + r"\s*=\s*'I'", norm_sql, re.IGNORECASE):
            v.append("Missing filter T0.\"DocType\" = 'I'.")
        if not re.search(_C("T0", "CANCELED") + r"\s*=\s*'N'", norm_sql, re.IGNORECASE):
            v.append("Missing filter T0.\"CANCELED\" = 'N'.")
        if not re.search(_C("T1", "TREETYPE") + r"\s*<>\s*'S'", norm_sql, re.IGNORECASE):
            v.append("Must exclude bundles: T1.\"TreeType\" <> 'S'.")
        if re.search(_C("T0", "DOCTOTAL"), code_upper):
            v.append("RULE VIOLATION: Never use T0.\"DocTotal\". Use SUM(T1.\"LineTotal\").")

        # PAI_LIVE_1 Specific Invoice Filters
        if company_id == "PAI_LIVE_1":
            raw_upper = norm_sql.upper()
            if "DEFECT" not in raw_upper:
                v.append("PAI_LIVE_1 Rule Violation: Must exclude defective warehouses (e.g., LOWER(OWHS.\"WhsName\") NOT LIKE '%defect%').")
            if "CARRY" not in raw_upper:
                v.append("PAI_LIVE_1 Rule Violation: Must exclude carry bags/packing items (e.g., LOWER(T1.\"ItemCode\") NOT LIKE '%carry%').")

        # PAI_LIVE_1 Specific Invoice Filters
        if company_id == "PAI_LIVE_1":
            raw_upper = norm_sql.upper()
            if "DEFECT" not in raw_upper:
                v.append("PAI_LIVE_1 Rule Violation: Must exclude defective warehouses (e.g., LOWER(OWHS.\"WhsName\") NOT LIKE '%defect%').")
            if "CARRY" not in raw_upper:
                v.append("PAI_LIVE_1 Rule Violation: Must exclude carry bags/packing items (e.g., LOWER(T1.\"ItemCode\") NOT LIKE '%carry%').")
    return norm_sql, v

def slice_rows(result: Any, n: int = 25) -> list:
    if isinstance(result, list):
        return result[:n]
    return []