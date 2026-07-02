import requests
import sys


URL = "http://127.0.0.1:8000/sql"
AUTH = ("root", "root")


def run_sql_batch(statements, label=""):
    sql = "USE NS strata DB strata;\n" + ";\n".join(statements) + ";"
    headers = {"Accept": "application/json", "Content-Type": "text/plain"}
    response = requests.post(URL, data=sql, headers=headers, auth=AUTH, timeout=60)
    data = response.json()
    errors = []
    if isinstance(data, list):
        for i, item in enumerate(data):
            if item.get("status") == "ERR":
                msg = item.get("information") or item.get("result") or ""
                errors.append(f"{i}: {msg}")
    return data, errors


def run_single(sql):
    data, errors = run_sql_batch([sql])
    if errors:
        return None, errors
    if isinstance(data, list) and len(data) > 1:
        result = data[1].get("result")
        if isinstance(result, list) and result:
            return result[0], []
        return result, []
    return None, errors


def load_file(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    lines = content.split("\n")
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("--") or stripped.startswith("//"):
            continue
        if "--" in stripped:
            stripped = stripped.split("--")[0].strip()
        if stripped:
            clean_lines.append(stripped)
    content_no_comments = " ".join(clean_lines)

    statements = []
    current = ""
    depth = 0
    for part in content_no_comments.split(";"):
        part = part.strip()
        if not part:
            continue
        current += part + ";"
        depth += part.count("{") - part.count("}")
        if depth <= 0:
            statements.append(current.strip())
            current = ""
    if current.strip():
        statements.append(current.strip())
    return statements


def ensure_entity_schema():
    """Validate that the entity table accepts CREATEs. If not, re-apply DEFINE FIELDs."""
    test_name = "__schema_test_delme__"
    # Test with embedding too, because _ensure_entity embeds every entity
    # Use 384 dims to match HNSW index (vector dimension must match)
    real_emb = "[" + ",".join("0.1" for _ in range(384)) + "]"
    result, errors = run_single(f"CREATE entity SET name = '{test_name}', type = 'organization', embedding = {real_emb};")

    if errors:
        print(f"\n⚠️  Entity table schema incomplete — re-applying DEFINE FIELDs...")
        fields_sql = [
            "DEFINE FIELD OVERWRITE name ON entity TYPE string",
            "DEFINE FIELD OVERWRITE type ON entity TYPE string",
            "DEFINE FIELD OVERWRITE embedding ON entity TYPE option<array>",
            "DEFINE FIELD OVERWRITE metadata ON entity TYPE option<object>",
            "DEFINE FIELD OVERWRITE forgotten ON entity TYPE bool DEFAULT false",
            "DEFINE FIELD OVERWRITE forget_reason ON entity TYPE option<string>",
            "DEFINE FIELD OVERWRITE created_at ON entity TYPE datetime DEFAULT time::now()",
            "DEFINE FIELD OVERWRITE updated_at ON entity TYPE datetime DEFAULT time::now()",
        ]
        _, ferrors = run_sql_batch(fields_sql, "entity-fields")
        for err in ferrors:
            if "already exists" not in err:
                print(f"   ⚠️  {err}")

        # Retry
        result, errors = run_single(f"CREATE entity SET name = '{test_name}', type = 'organization', embedding = {real_emb};")
        if errors:
            print(f"   ❌ Entity schema still broken: {errors}")
            sys.exit(1)
        print(f"   ✅ Entity schema repaired")

    # Cleanup test entity (ignore errors)
    run_single(f"DELETE entity WHERE name = '{test_name}';")


if __name__ == "__main__":
    print("=== Loading schema optimized...")

    all_statements = []
    for f in ["docs/schema.surql", "docs/helper_functions.surql", "docs/test_data.surql"]:
        stmts = load_file(f)
        print(f"   {f}: {len(stmts)} statements")
        all_statements.extend(stmts)

    batch_size = 5
    for i in range(0, len(all_statements), batch_size):
        batch = all_statements[i:i + batch_size]
        batch_num = i // batch_size + 1
        print(f"   Executing batch {batch_num}...", end=" ", flush=True)
        try:
            _, errors = run_sql_batch(batch)
            if errors:
                print(f"⚠️  {errors}")
            else:
                print("✅ OK")
        except Exception as e:
            print(f"❌ Error: {e}")
            print(f"   Batch content: {batch}")

    print("\n✅ All loaded!")

    ensure_entity_schema()

    check, errors = run_sql_batch(["SELECT count() FROM event", "SELECT count() FROM entity"])
    print(f"\n🔍 Quick check:")
    if errors:
        print(f"   ⚠️  Errors: {errors}")
    else:
        event_count = entity_count = "?"
        for item in (check or []):
            if isinstance(item, dict) and item.get("status") == "OK":
                res = item.get("result")
                if isinstance(res, list) and len(res) > 0 and isinstance(res[0], dict):
                    if "database" in res[0]:
                        continue
                    elif res[0].get("count") is not None:
                        if event_count == "?":
                            event_count = res[0]["count"]
                        else:
                            entity_count = res[0]["count"]
        print(f"   Events: {event_count}")
        print(f"   Entities: {entity_count}")
