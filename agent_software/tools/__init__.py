from .query_sql import query_sql_schema, query_sql_func
from .policy_lookup import policy_lookup_schema,policy_lookup_func
from .inspect_schema import inspect_schema_schema, inspect_schema_func

all_tools_schema = [
    query_sql_schema,
    policy_lookup_schema,
    inspect_schema_schema,
]

available_functions = {
    "query_sql": query_sql_func,
    "policy_lookup": policy_lookup_func,
    "inspect_schema": inspect_schema_func,
}
